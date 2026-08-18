import asyncio
import functools
import json
import logging
import html
from datetime import datetime

from aiogram import Bot, F, Router, types
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
import os

from aiogram.types import FSInputFile, ReplyKeyboardRemove, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import ReplyKeyboardBuilder

from config import config
from database.db import add_user, get_user, get_setting, set_setting, mark_reg_started, clear_reg_started, set_reg_step, set_user_subscribed, set_user_status, record_user_consent, get_user_consents, get_reg_started_track, get_reg_started_city, has_short_incomplete, _csv_safe, get_incomplete_rows_with_city, reset_payment_for_new_season
from settings_schema import SETTINGS_SCHEMA, get_setting_typed  # REG-01/D-06 (06-04): REG_DEFAULTS derivation source; get_setting_typed (06-06 gate migration)
from cities import CITIES, all_cities, normalize_city, is_default_city, city_tab_base, cities_module_on, is_city_enabled, city_label, enabled_cities, tab_suffix, get_setting_for_city, get_setting_typed_for_city  # Phase 07.1 (CITY-01/CITY-02/CITY-03): city registry — _city_tag_map() + city_row_tab + city fork below; tab_suffix added quick 260815-3hw (TABS-01/02/03, replaces the raw TAB_SUFFIX import); get_setting_for_city/get_setting_typed_for_city added Phase 09.2-04 (CITY-04): per-city text/mode resolver; all_cities added Phase 14 (CITY-07)
from handlers.states import Registration
from keyboards.builders import (
    get_main_menu_kb,
    get_cancel_kb,
    get_confirm_kb,
    get_skip_kb,
    get_phone_kb,
    get_local_committee_kb,
    get_position_kb,
    get_department_kb,
    get_aiesec_role_kb,
    get_english_level_kb,
    get_arrival_kb,
    get_housing_kb,
    get_attendance_format_kb,
    get_informal_day_kb,
    get_source_kb,
    get_education_status_kb,
    get_universities_kb,
    get_course_kb,
    get_yes_no_kb,
)
from services.sheets import append_to_sheet, append_to_named_sheet
from services.nextcloud import upload_resume, upload_text_resume
from services.background import spawn as _spawn
from handlers.admin_caps import notify_by_capability  # D-13: fan out by capability, not bare ADMIN_IDS

router = Router()
logger = logging.getLogger(__name__)

# Ночное ревью, находка #3: telegram_id, для которых finalize_registration прямо сейчас
# выполняется. Проверка `uid in ...` и `.add(uid)` в обёртке идут БЕЗ единого await между
# ними, поэтому в однопоточном asyncio они атомарны — пара get_data()/update_data() поверх
# FSM такой гарантии не дала бы. Персистентность не нужна и вредна: это гвард живого
# in-flight вызова, а не состояние; после рестарта ни одного вызова в полёте нет.
_FINALIZING_USERS: set[int] = set()

DEFAULT_START_TEXT = (
    "Привет! \U0001f44b\n\n"
    "Это бот мероприятия. Зарегистрируйся, чтобы получить доступ ко всей информации.\n\n"
    "Настройте текст приветствия через /admin → Настройки → Приветствие."
)

# UAT 17.08: вернувшемуся участнику нельзя показывать текст для новичков («заявка займёт
# 5-7 минут…») — тестировщики решили, что регистрация сбросилась. Настраивается ключом
# start_text_registered (/admin → Настройки → Событие/Медиа).
DEFAULT_START_REGISTERED_TEXT = (
    "С возвращением! Ты уже зарегистрирован(а) — всё нужное в меню ниже \U0001f447"
)

# Phase 07.3 (RET-02): a delegate whose row belongs to a PAST season (or who was rejected) is
# neither a newcomer nor a currently-registered delegate — cmd_start's already-registered
# branch below would otherwise dead-end them (rejected today falls through silently into a
# fresh registration; a past-season pending/approved row hits a hard `return`). `{season}` is
# substituted with the delegate's own past-season label via str.replace, never `.format()` —
# an admin-authored registry text may contain other braces that `.format()` would choke on.
DEFAULT_START_RETURNING_TEXT = (
    "С возвращением! Ты уже был(а) с нами на {season}. Давай обновим анкету — "
    "большинство ответов уже заполнено, останется только подтвердить \U0001f447"
)

# Tatiana: «поздравляем» теперь приходит СРАЗУ после регистрации (раньше — только после
# одобрения). reg_complete_text = пост-регистрационный скрипт; approve_text = отдельный
# скрипт после одобрения заявки. Оба правятся в /admin → Настройки.
DEFAULT_REG_COMPLETE_TEXT = (
    "Поздравляем, твоя заявка принята!\n\n"
    "Мы рассмотрим её в течение 2-3 дней и напишем сюда. "
    "Следи за обновлениями, впереди много интересного.\n\n"
    "Если у тебя возникнут вопросы — не стесняйся задавать их нам!"
)
DEFAULT_APPROVE_TEXT = "Твоя заявка одобрена! Добро пожаловать 🎉"

# --- Approval status decision (Phase 2, D-01..D-03) ---

def _decide_status(reg_mode: str, full_setting: str, short_setting: str,
                    participant_type: str = "full", party_setting: str | None = None) -> str:
    """Form type x per-form moderation setting -> 'pending' | 'approved'.
    Full form uses full_setting, short form uses short_setting; 'manual' -> pending.

    Phase 5 (D-13): party tracks resolve status from party_approval alone, completely
    independent of full_approval/short_approval — this branch never falls through to the
    reg_mode logic below it, and never reads full_setting/short_setting. `party_setting`
    of None (an unconfigured party_approval) resolves to "manual": a party track must be
    moderated by default, never silently auto-approved (T-05-04-02).

    Phase 7 (SHORT-05): the short track resolves status from `short_setting` (short_approval)
    alone, keyed off the persisted `participant_type`, and — crucially — WITHOUT reading
    `reg_mode` at all. Before this phase the short form was answered in one message, so the
    gap between flow-start and finalize() (where `reg_mode` is re-read live) was ~0 and the
    old `reg_mode`-based lookup was harmless. Task 2 turns the short form into a multi-question
    conversation, so that gap can now span the whole promo window: a delegate who starts the
    form on 2026-08-10 and finishes after the manager flips `registration_mode` back to "full"
    on 2026-08-11 would otherwise be moderated by `full_approval` instead of `short_approval` —
    a direct SHORT-05 violation. Branching on the immutable `participant_type` instead of the
    live `reg_mode` setting is the same decoupling technique D-13 already used for party."""
    if _is_party_track(participant_type):
        setting = party_setting or "manual"
        return "pending" if setting == "manual" else "approved"
    if _is_short_track(participant_type):
        return "pending" if short_setting == "manual" else "approved"
    setting = full_setting if reg_mode == "full" else short_setting
    return "pending" if setting == "manual" else "approved"


# --- Registration Flow Engine ---

# Phase 4 (D-07): each entry is (step_key, setting_key, type). type is "text" (default
# free-text handler), "date" (ДД.ММ.ГГГГ validation), or "consent" (injected dynamically,
# never declared statically here). Iteration sites star-unpack the type so 2-tuples would
# still work during any incremental migration.
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

# Map step_key → type for O(1) dispatch in _ask_step (consent:* keys handled separately).
REG_STEP_TYPES = {step_key: step_type for step_key, _sk, step_type in REG_FLOW}

# step_key → its reg_q_* setting key, for resolving human labels in dropout analytics.
_STEP_TO_SETTING = {step_key: setting_key for step_key, setting_key, _t in REG_FLOW}

# Phase 07.3 (04, RET-02): step_key -> users column name. EXPLICIT map, not derived from
# step_key identity (RESEARCH's own anti-pattern warning) -- most REG_FLOW keys match their
# column 1:1, but "vk" writes vk_username and "ambassador" writes is_ambassador_candidate
# (registration.py process_vk / process_ambassador_yesno). "resume" stays in the map as an
# identity entry (three real columns: resume_file_id/resume_text/resume_url) but is excluded
# from RECALLABLE_STEPS -- it gets its own carve-out branch in _ask_step_or_recall/recall_keep
# because showing a raw file_id to a human is meaningless (RESEARCH Pitfall 3).
STEP_TO_COLUMN = {step_key: step_key for step_key, _sk, _t in REG_FLOW}
STEP_TO_COLUMN["vk"] = "vk_username"
STEP_TO_COLUMN["ambassador"] = "is_ambassador_candidate"
RECALLABLE_STEPS = {k for k in STEP_TO_COLUMN if k != "resume"}


def dropout_step_label(step_key: str | None) -> str:
    """Human label for a persisted last_step (dropout analytics). Handles the special
    pre-flow steps (ФИО / consent) and falls back to the raw key if unmapped."""
    if not step_key:
        return "— (не начал отвечать)"
    if step_key == "full_name":
        return "🪪 ФИО"
    if step_key.startswith("consent:"):
        return "📋 Согласие"
    setting_key = _STEP_TO_SETTING.get(step_key)
    if setting_key:
        # REG_LABELS is defined below but only read at call time, so the forward ref is fine.
        return REG_LABELS.get(setting_key, step_key)
    return step_key

# Configurable single-select steps: step_key → (options_setting_key, default options).
# Options edited in admin as newline text (reuse source_options pattern). "Другое" appended.
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


async def _get_options(setting_key: str, defaults: list[str]) -> list[str]:
    """Admin-editable option list (newline text) with a hardcoded fallback."""
    raw = await get_setting(setting_key)
    if raw:
        items = [line.strip() for line in raw.splitlines() if line.strip()]
        if items:
            return items
    return list(defaults)

# REG-01/D-06 (06-04): REG_DEFAULTS is now DERIVED from settings_schema.SETTINGS_SCHEMA
# (every registered "toggle"-type entry), not a hand-maintained literal — the registry is
# the single source of truth for reg_q_* defaults (T-06-12/T-06-13, test_reg_defaults_parity
# pins byte-for-byte parity against the pre-migration 43-key literal). The NAME is retained
# unchanged because handlers/admin.py still imports/iterates it (admin.py:59 import, :501,
# :2097 _is_question_on, :2248, and the preset bulk-write loop at :2336/:2412) — deleting the
# name would break those call sites for no benefit; only the VALUE's origin changed.
# settings_schema imports only database.db (one-directional, T-06-14) — importing it here
# does not create a cycle.
REG_DEFAULTS = {
    k: v["default"] for k, v in SETTINGS_SCHEMA.items() if v["type"] == "toggle"
}

REG_LABELS = {
    "reg_q_age": "\U0001f382 Возраст",
    "reg_q_vk": "\U0001f535 ВК",
    "reg_q_email": "\U0001f4e7 Email",
    "reg_q_phone": "\U0001f4f1 Телефон",
    "reg_q_city": "\U0001f3d9 Город",
    "reg_q_source": "\U0001f4e2 Источник",
    "reg_q_lc": "\U0001f3e2 Лок. комитет",
    "reg_q_position": "\U0001f454 Позиция",
    "reg_q_education": "\U0001f393 Образование",
    "reg_q_university": "\U0001f3eb ВУЗ",
    "reg_q_course": "\U0001f4d6 Курс",
    "reg_q_specialty": "\U0001f4dd Специальность",
    "reg_q_work": "\U0001f4bc Работа",
    "reg_q_work_sphere": "\U0001f3ed Сфера работы",
    "reg_q_skills": "\U0001f4a1 Навыки",
    "reg_q_expectations": "\U0001f4ac Ожидания (общие)",
    "reg_q_informal_day": "\U0001f3d5 Неформальный день",
    "reg_q_attendance": "\U0001f4cd Формат",
    "reg_q_comments": "\U0001f4ac Доп. комментарии",
    "reg_q_department": "🏢 Департамент",
    "reg_q_aiesec_role": "🎖 Позиция AIESEC",
    "reg_q_certificate": "📄 Справка в ВУЗ",
    "reg_q_alumni_status": "🎓 Аламни/айсекер",
    "reg_q_english": "🇬🇧 Англ. язык",
    "reg_q_allergies": "🤧 Аллергии",
    "reg_q_food": "🥗 Питание",
    "reg_q_arrival": "🚌 Приезд",
    "reg_q_housing": "🏠 Проживание",
    "reg_q_bed_sharing": "🛏 Общая кровать",
    "reg_q_bed_partner": "🛏 Сосед по кровати",
    "reg_q_transport": "🚗 Трансфер",
    "reg_q_payment_date": "💳 Дата оплаты",
    "reg_q_cc_shop": "🛍 CC-shop",
    "reg_q_exp_organizers": "💬 Ожидания: организация",
    "reg_q_exp_content": "💬 Ожидания: контент",
    "reg_q_volunteer": "🙋 Волонтёр",
    "reg_q_arrival_date": "📅 Дата приезда",
    "reg_q_birth_date": "🎂 Дата рождения",
    "reg_q_study_field": "🎯 Направление обучения",
    "reg_q_goal": "🎯 Цель участия",
    "reg_q_formats": "📋 Форматы форума",
    "reg_q_ambassador": "🧡 Амбассадор",
    "reg_q_resume": "\U0001f4c4 Резюме",
}

# --- Event-type presets (admin one-tap bulk toggle) ---
# A preset lists the reg_q_* keys to turn ON (everything else in REG_DEFAULTS is turned
# OFF) plus the payment module flag. Applying a preset is an explicit admin action that
# writes the same settings the per-question toggles write — it changes NOTHING until
# tapped, so live bots keep their current flow. Extra questions can still be flipped on
# individually afterwards (see REG_CATEGORIES «➕ Экстра»).
REG_PRESETS = {
    "forum": {
        "label": "🏛 Форум (YouLead)",
        "payment_enabled": "off",
        "on": [
            "reg_q_age", "reg_q_vk", "reg_q_source", "reg_q_education",
            "reg_q_university", "reg_q_course", "reg_q_study_field", "reg_q_work",
            "reg_q_work_sphere", "reg_q_skills", "reg_q_expectations",
        ],
    },
    "conf": {
        "label": "🎤 Конференция (RusCo)",
        "payment_enabled": "on",
        "on": [
            "reg_q_age", "reg_q_vk", "reg_q_phone", "reg_q_lc", "reg_q_work",
            "reg_q_department", "reg_q_aiesec_role", "reg_q_english", "reg_q_allergies",
            "reg_q_food", "reg_q_arrival", "reg_q_bed_sharing", "reg_q_bed_partner",
            "reg_q_transport", "reg_q_payment_date",
            "reg_q_cc_shop", "reg_q_exp_organizers", "reg_q_volunteer",
        ],
    },
    "party": {
        "label": "🎉 Party",
        # Phase 5 (D-07): NO "payment_enabled" key here — the party preset must never touch
        # the payment module (party pricing is D-16/D-17 in plan 05-05, a separate concern).
        # setting_key spellings (not step_keys) — the shared confirm dialog in admin.py
        # renders REG_LABELS.get(k, k) for k in preset["on"], and REG_LABELS is keyed by
        # reg_q_*; matches the "forum"/"conf" entries above.
        "on": [
            "reg_q_age", "reg_q_phone", "reg_q_alumni_status", "reg_q_vk", "reg_q_city",
            "reg_q_allergies", "reg_q_food",
        ],
    },
    "short": {
        "label": "⚡ Акция: 6 вопросов",
        # Phase 7 (D-07 pattern): NO "payment_enabled" key here either — the promo preset
        # must never touch the payment module, same reasoning as the party preset above.
        # preset_apply already tolerates its absence via preset.get("payment_enabled").
        # Five setting_keys below + ФИО = six questions: ФИО is asked unconditionally by
        # _ask_full_name and is NOT a REG_FLOW key, so it can never appear in an "on" list —
        # it is not missing, it just isn't a toggle.
        "on": [
            "reg_q_phone", "reg_q_vk", "reg_q_city", "reg_q_education", "reg_q_course",
        ],
    },
}


# WR-03: the D-08 overnight-only questions are excluded from _apply_party_preset's blanket
# on/off pass. They are governed by the participant_type == 'party_overnight' skip rule in
# _get_enabled_steps, not by the preset — writing an explicit __party=off override for them
# would force them off even in a conf-style deployment where they are enabled globally,
# defeating D-08 entirely. Left at inherit (no __party key written) so that rule keeps
# working exactly as it does for any other config that never touched the preset.
_PARTY_PRESET_OVERNIGHT_EXEMPT = {"reg_q_housing", "reg_q_bed_sharing", "reg_q_bed_partner"}


async def _apply_party_preset() -> None:
    """D-07: bulk-write __party overrides ONLY — mirrors _apply_event_preset's
    determinism guarantee (handlers/admin.py:2040-2048) but targets the __party
    namespace exclusively. Every REG_FLOW step EXCEPT the D-08 overnight-only trio
    (_PARTY_PRESET_OVERNIGHT_EXEMPT, WR-03) gets an explicit on/off __party key, so
    re-tapping the preset is deterministic regardless of prior manual overrides. Matches
    on setting_key, consistent with the "on" list spelling above — matching on step_key
    while the list holds setting_keys would silently write "off" for every question.

    This function's ONLY write is to __party-suffixed keys (setting_key + "__party");
    it never writes a bare reg_q_* key, never touches payment_enabled/party_enabled/
    party_approval/party_fork_question, and never calls delete_setting. That isolation
    is the entire point of D-07: applying the party preset while a full delegate is
    mid-registration cannot alter that delegate's question set (T-05-02-01)."""
    on_set = set(REG_PRESETS["party"]["on"])
    for _step_key, setting_key, *_rest in REG_FLOW:
        if setting_key in _PARTY_PRESET_OVERNIGHT_EXEMPT:
            continue
        await set_setting(f"{setting_key}__party", "on" if setting_key in on_set else "off")


async def _apply_short_preset() -> None:
    """Phase 7 (SHORT-03): bulk-write __short overrides ONLY — mirrors _apply_party_preset's
    determinism guarantee, targets the __short namespace exclusively. Unlike the party preset,
    there is no exempt set here: _PARTY_PRESET_OVERNIGHT_EXEMPT exists purely to protect D-08's
    participant_type == 'party_overnight' skip rule, which is gated on _is_party_track and
    therefore structurally cannot fire for the short track — every REG_FLOW key gets an
    explicit on/off write, making a repeated tap fully deterministic.

    This function's ONLY write is to __short-suffixed keys (setting_key + "__short"); it
    never writes a bare reg_q_* key, never touches __party, and never touches
    payment_enabled/registration_mode. That isolation means applying the promo preset mid-
    registration cannot alter a full or party delegate's question set."""
    on_set = set(REG_PRESETS["short"]["on"])
    for _step_key, setting_key, *_rest in REG_FLOW:
        await set_setting(f"{setting_key}__short", "on" if setting_key in on_set else "off")


# Display grouping for the admin question-toggle view. Disjoint buckets covering every
# REG_FLOW key exactly once — purely cosmetic (helps the manager find a question), does
# not affect which questions are asked (that is REG_DEFAULTS + per-key settings).
REG_CATEGORIES = [
    ("👥 Общие", ["reg_q_age", "reg_q_vk", "reg_q_work"]),
    ("🏛 Форум", [
        "reg_q_education", "reg_q_course", "reg_q_university", "reg_q_study_field",
        "reg_q_expectations", "reg_q_source", "reg_q_work_sphere", "reg_q_skills",
    ]),
    ("🎤 Конфа", [
        "reg_q_phone", "reg_q_lc", "reg_q_department", "reg_q_aiesec_role",
        "reg_q_alumni_status",
        "reg_q_english", "reg_q_allergies", "reg_q_food", "reg_q_arrival",
        "reg_q_bed_sharing", "reg_q_bed_partner",
        "reg_q_transport", "reg_q_cc_shop", "reg_q_exp_organizers", "reg_q_volunteer",
        "reg_q_payment_date",
    ]),
    ("➕ Экстра", [
        "reg_q_city", "reg_q_goal", "reg_q_formats", "reg_q_ambassador", "reg_q_resume",
        "reg_q_email", "reg_q_position", "reg_q_specialty", "reg_q_attendance",
        "reg_q_informal_day", "reg_q_comments", "reg_q_certificate", "reg_q_housing",
        "reg_q_exp_content", "reg_q_arrival_date", "reg_q_birth_date",
    ]),
]


async def _prompt(step_key: str, default: str, participant_type: str | None = None) -> str:
    """Editable question wording: admin override reg_prompt_<step_key> else the default.
    Lets organizers set exact per-event wording (YL'26/Summit/…) without code changes.
    Phase 5 (D-05): party tracks check reg_prompt_<step_key>__party FIRST (truthy wins),
    else fall through to the existing global resolution unchanged. `participant_type`
    defaults to None so every pre-Phase-5 two-arg call site keeps compiling and behaving
    identically — only _ask_step (which resolves the track once) passes the third arg.
    Truthiness (not `is not None`) is deliberate here: an accidentally-empty override
    string must fall back to the global text rather than send a blank message — this
    differs from the gate resolver (_is_step_enabled_for_track), where `is not None` is
    required to keep "off" distinguishable from "inherit"."""
    if _is_party_track(participant_type):
        override = await get_setting(f"reg_prompt_{step_key}__party")
        if override:
            return override
    return await get_setting(f"reg_prompt_{step_key}") or default


async def _is_step_enabled(setting_key: str) -> bool:
    val = await get_setting(setting_key)
    if val is None:
        return REG_DEFAULTS.get(setting_key, "on") == "on"
    return val == "on"


async def _is_step_enabled_for_track(setting_key: str, participant_type: str | None) -> bool:
    """D-03/D-04: tri-state per-track gate. `__party` is ONE namespace shared by
    party_overnight and party_noovernight — there is no per-sub-track key split.
    Key-absence means "inherit the global reg_q_<step> value"; key-presence means the
    explicit on/off wins, regardless of what the global value is. The `is not None` check
    is load-bearing: collapsing None into a boolean early would make "inherit"
    indistinguishable from "off" and would break the admin tri-state cycle (plan 05-03).
    Full track (or None) never reads the __party key — byte-identical to _is_step_enabled.

    Phase 7 (SHORT-01/SHORT-04): the short track reads `{setting_key}__short` the same
    tri-state way (`is not None`, never truthiness — same load-bearing reason as above, so
    the admin tri-state cycle for `__short` keys works identically to `__party`). It
    deliberately does NOT fall through to the global `_is_step_enabled` on key-absence: party
    is a curated subset of the ALREADY-live full form, so "inherit the global set" is the
    right default there. Short exists specifically to NOT be the global set — inheriting it
    would make the promo form equal to the full form again, which is the exact bug this phase
    fixes, and would silently break SHORT-04 (manager would have to turn off ~14 global
    toggles by hand). So for short, key-absence means "do not ask this question", full stop.
    """
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


async def _is_module_enabled(key: str) -> bool:
    """Phase 4 module flag check — None/absent/'off'/anything-but-'on' → False (D-15 fail-safe).
    # REG-02 (06-06, BLOCKER-2): resolved via the registry — consent_enabled/payment_enabled
    # both default "off" (06-04), so get_setting_typed returns "off" for None AND "" and
    # `== "on"` stays False, byte-identical to the old None->False fail-safe."""
    return await get_setting_typed(key) == "on"


async def _get_enabled_steps(data: dict) -> list[str]:
    enabled = []
    # edu_conditional (default on): skip ВУЗ/курс/специальность when "не учусь". Turn OFF
    # for events (e.g. YL'26) that ask образование as a level, not a Да/Нет, and want those
    # steps always shown.
    edu_conditional = await get_setting_typed("edu_conditional") == "on"  # REG-02: registry-backed
    studying = str(data.get("education_status", "")).startswith("Да")
    participant_type = data.get("participant_type") or "full"  # Phase 5 (D-03/D-04/D-08)
    for step_key, setting_key, *_rest in REG_FLOW:
        if not await _is_step_enabled_for_track(setting_key, participant_type):
            continue
        if step_key == "informal_day" and data.get("attendance_format") == "Online":
            continue
        # Source came from a src_ deep-link tag — don't ask «Откуда узнал», it's authoritative.
        if step_key == "source" and data.get("_source_from_tag"):
            continue
        # Tatiana: «Где будешь жить» — только если приезжает Заранее (в дни конфы жильё не нужно).
        # Backward-safe: gate only when arrival was actually asked; else housing stays unconditional.
        if step_key == "housing" and "arrival" in data and data.get("arrival") != "Заранее":
            continue
        # «С кем на кровати» спрашиваем только если согласился делить (bed_sharing = «Да»).
        if step_key == "bed_partner" and not str(data.get("bed_sharing", "")).startswith("Да"):
            continue
        # Phase 5 (D-08): housing/bed_sharing/bed_partner reuse the SAME steps, gated to the
        # overnight party sub-track only. No new step keys, no new DB/sheet columns (D-08/D-09).
        # Written so it can never fire for "full"/None — only excludes party tracks that are
        # NOT party_overnight (i.e. party_noovernight).
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
    # Consents run BEFORE ФИО now (handled in _start_registration_flow /
    # process_consent_accept), so they are no longer part of the question engine.
    return enabled


# Fallback when consent_enabled is on but consent_list is empty: one «обработка ПД» consent
# (ссылки на документы уже в приветственном сообщении, поэтому хватает одной кнопки).
DEFAULT_CONSENTS = [("Согласие на обработку персональных данных", "personal_data")]


async def _consent_entries() -> list[tuple[str, str]]:
    """Parse consent_list ('Видимое название | ключ' per line) → [(label, key)].
    Empty/invalid list → DEFAULT_CONSENTS. Shared by step-building and rendering so the
    label/key stay in sync."""
    raw = await get_setting("consent_list") or ""
    entries: list[tuple[str, str]] = []
    # Accept ';' as a line separator too: on mobile Telegram Enter=send splits a
    # multi-line entry into separate messages (only the first survives set_setting),
    # so admins can put all consents on one line using ';'. Newline data unaffected.
    for line in raw.replace(";", "\n").strip().splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        label, consent_key = line.split("|", 1)
        consent_key = consent_key.strip()
        if consent_key:
            entries.append((label.strip(), consent_key))
    return entries or DEFAULT_CONSENTS


async def _get_consent_steps() -> list[str]:
    """Consent step keys (consent:<key>) when consent_enabled is on, else []. Shared by
    the full-form engine and the short-form path so consents fire regardless of form length."""
    if not await _is_module_enabled("consent_enabled"):
        return []
    return [f"consent:{key}" for _label, key in await _consent_entries()]


async def _payment_price_block() -> str:
    """Price list prepended to the payment-date question so the user sees the cost(s)
    while picking a plan date (prices otherwise appear only post-approval, in
    handlers.payment). Empty when the payment module is off or no options are set.
    Informational only — tariff selection still happens after approval."""
    if not await _is_module_enabled("payment_enabled"):
        return ""
    from handlers.payment import _parse_options  # local import avoids circular import
    options = _parse_options(await get_setting("payment_options") or "")
    if not options:
        return ""
    lines = []
    for label, price, _tracks in options:
        # Phase 5: this is an informational pre-approval price preview shown to every
        # visitor regardless of track — filtering by track happens post-approval in
        # handlers.payment (D-17); the third element is unused here, not this task's scope.
        price_txt = f"{price} ₽" if price > 0 else "бесплатно"
        lines.append(f"• {html.escape(label)} — {price_txt}")
    return "💳 <b>Стоимость участия:</b>\n" + "\n".join(lines) + "\n\n"


# Лимит текста одного сообщения в Telegram Bot API. Всё, что длиннее, отвергается
# с 400 — а раздутый summary регистрации (находка #1) как раз может его перебить.
_TG_TEXT_LIMIT = 4096
_TRUNC_SUFFIX = "\n\n… (сообщение сокращено)"


async def _safe_answer(message: types.Message, text: str, **kwargs):
    """Отправить текст так, чтобы отправка НИКОГДА не подняла исключение.

    Две причины существования (ночное ревью, находки #1 и #2):

    1. HTML-фолбэк. main.py ставит глобальный parse_mode=HTML, поэтому любой текст,
       собранный из введённых менеджером настроек (reg_prompt_*, event_name), может не
       пройти разбор entities. Повторяем ту же отправку без разметки: делегат увидит
       текст, пусть и без форматирования, — это лучше, чем не увидеть ничего.
    2. Код ПОСЛЕ отправки обязан выполниться. В _ask_step/_advance сразу за отправкой идёт
       state.set_state(...). Исключение из answer() прерывало функцию до перехода, а
       глобальный @dp.errors() (main.py) его молча глотал — делегат оставался заперт на
       прежнем шаге, без вопроса и без ошибки. Поэтому хелпер возвращает None вместо того,
       чтобы падать, и порядок «спросил → перешёл» сохраняется без перестановки строк.

    Слишком длинный текст режется ДО первой отправки: ретрай без разметки её не спас бы
    (лимит не про HTML), а усечённый summary + клавиатура подтверждения лучше вечного клина.
    """
    if text is not None and len(text) > _TG_TEXT_LIMIT:
        logger.warning(
            f"Message to {getattr(getattr(message, 'chat', None), 'id', '?')} truncated: "
            f"{len(text)} > {_TG_TEXT_LIMIT}"
        )
        text = text[: _TG_TEXT_LIMIT - len(_TRUNC_SUFFIX)] + _TRUNC_SUFFIX
    try:
        return await message.answer(text, **kwargs)
    except Exception as e:
        logger.warning(
            f"answer() to {getattr(getattr(message, 'chat', None), 'id', '?')} failed "
            f"({e}) — retrying without parse_mode"
        )
    plain = dict(kwargs)
    plain["parse_mode"] = None
    try:
        return await message.answer(text, **plain)
    except Exception as e:
        logger.error(
            f"answer() to {getattr(getattr(message, 'chat', None), 'id', '?')} failed twice: {e}"
        )
        return None


async def _stamp_reg_step(step_key: str, message: types.Message, data: dict) -> None:
    """Dropout analytics + quick k4y: stamp the question about to be shown AND snapshot the
    already-answered fields (FSM data minus internal `_`-prefixed bookkeeping keys) so the
    «Незавершённые» sheet tab can show what the delegate has filled in so far. message.chat.id
    == the user's id in private chats (holds for both Message and callback.message). Fail-soft:
    any serialization error is logged and never blocks the question from being asked.

    Phase 07.3 (04): extracted verbatim from _ask_step's head so _ask_step_or_recall's recall
    screen can stamp the same way -- otherwise «Незавершённые» would lose track of where a
    returning delegate stopped."""
    try:
        snapshot = {k: v for k, v in data.items() if not k.startswith("_")}
        partial_json = json.dumps(snapshot, ensure_ascii=False, default=str)
        await set_reg_step(message.chat.id, step_key, partial_json)
    except Exception as e:
        logger.error(f"set_reg_step failed for {message.chat.id} @ {step_key}: {e}")


def _recall_display(step_key: str, value) -> str:
    """Human-readable rendering of a prior answer for the recall screen. Booleans (Python
    True/False, or SQLite's int 0/1 -- aiosqlite returns is_ambassador_candidate as an int,
    not a bool, since SQLite has no native boolean type) render as «Да»/«Нет»; everything else
    is str()'d. The result is HTML-escaped here (not by the caller) -- the value came out of
    the users row and is about to land inside an HTML message (T-073-04-02)."""
    if isinstance(value, bool) or (isinstance(value, int) and value in (0, 1)):
        return "Да" if value else "Нет"
    return html.escape(str(value))


async def _ask_step(step_key: str, message: types.Message, state: FSMContext, step: int, total: int):
    # Phase 5 (D-05): resolve the track once, same as _get_enabled_steps, and pass it to
    # every _prompt call below so party-track wording overrides resolve correctly.
    data = await state.get_data()
    participant_type = data.get("participant_type") or "full"
    await _stamp_reg_step(step_key, message, data)
    p = await _progress(step, total)
    if step_key == "age":
        await _safe_answer(message, f"{p}{await _prompt('age', 'Напиши свой возраст числом:', participant_type)}", reply_markup=get_cancel_kb())
        await state.set_state(Registration.age)
    elif step_key == "email":
        await _safe_answer(message, f"{p}{await _prompt('email', 'Укажи свой email:', participant_type)}", reply_markup=get_cancel_kb())
        await state.set_state(Registration.email)
    elif step_key == "phone":
        await _safe_answer(message, f"{p}{await _prompt('phone', 'Укажи номер телефона:', participant_type)}", reply_markup=get_phone_kb())
        await state.set_state(Registration.phone)
    elif step_key == "vk":
        await _safe_answer(message,
            f"{p}{await _prompt('vk', 'Введи свой ник в ВК в формате @username:', participant_type)}",
            reply_markup=get_cancel_kb(),
        )
        await state.set_state(Registration.vk)
    elif step_key == "city":
        opt_key, default = SELECT_CONFIG["city"]
        options = await _get_options(opt_key, default)
        await _safe_answer(message,
            f"{p}{await _prompt('city', 'Из какого ты города?', participant_type)}",
            reply_markup=_reply_kb(options, add_other=True),
        )
        await state.set_state(Registration.city)
    elif step_key == "source":
        await _safe_answer(message, f"{p}{await _prompt('source', 'Откуда ты узнал(а) о нас?', participant_type)}", reply_markup=await get_source_kb())
        await state.set_state(Registration.source)
    elif step_key == "local_committee":
        await _safe_answer(message, f"{p}{await _prompt('local_committee', 'Локальный комитет:', participant_type)}", reply_markup=get_local_committee_kb())
        await state.set_state(Registration.local_committee)
    elif step_key == "position":
        await _safe_answer(message, f"{p}{await _prompt('position', 'Твоя позиция:', participant_type)}", reply_markup=get_position_kb())
        await state.set_state(Registration.position)
    elif step_key == "education_status":
        await _safe_answer(message, f"{p}{await _prompt('education_status', 'Учишься ли ты сейчас?', participant_type)}", reply_markup=get_education_status_kb())
        await state.set_state(Registration.education_status)
    elif step_key == "university":
        # Mode toggle (reg_university_mode): "list" = pick from база вузов, "text" = free input.
        mode = await get_setting_typed("reg_university_mode")  # REG-02: registry-backed
        if mode == "text":
            await _safe_answer(message, f"{p}{await _prompt('university', 'Введи название твоего ВУЗа:', participant_type)}", reply_markup=get_skip_kb())
        else:
            uni_opts = await get_setting("university_options")
            if uni_opts and uni_opts.strip():
                options = [l.strip() for l in uni_opts.splitlines() if l.strip()]
                kb = _reply_kb(options, add_other=True)
            else:
                kb = get_universities_kb()  # fallback: config.UNIVERSITIES
            await _safe_answer(message, f"{p}{await _prompt('university', 'В каком ВУЗе/колледже ты учишься?', participant_type)}", reply_markup=kb)
        await state.set_state(Registration.university)
    elif step_key == "course":
        await _safe_answer(message, f"{p}{await _prompt('course', 'На каком ты курсе?', participant_type)}", reply_markup=get_course_kb())
        await state.set_state(Registration.course)
    elif step_key == "specialty":
        await _safe_answer(message, f"{p}{await _prompt('specialty', 'Какая у тебя специальность?', participant_type)}", reply_markup=get_skip_kb())
        await state.set_state(Registration.specialty)
    elif step_key == "work_status":
        await _safe_answer(message, f"{p}{await _prompt('work_status', 'Работаешь ли ты сейчас?', participant_type)}", reply_markup=get_yes_no_kb())
        await state.set_state(Registration.work_status)
    elif step_key == "work_sphere":
        await _safe_answer(message, f"{p}{await _prompt('work_sphere', 'В какой сфере ты работаешь?', participant_type)}", reply_markup=get_skip_kb())
        await state.set_state(Registration.work_sphere)
    elif step_key == "missing_skills":
        await _safe_answer(message, f"{p}{await _prompt('missing_skills', 'Каких навыков тебе сейчас не хватает?', participant_type)}", reply_markup=get_skip_kb())
        await state.set_state(Registration.missing_skills)
    elif step_key == "expectations":
        event_name = await get_setting("event_name") or "мероприятия"
        await _safe_answer(message,
            f"{p}{await _prompt('expectations', f'Что ты ожидаешь от {event_name}? Что хотел(а) бы узнать или получить?', participant_type)}",
            reply_markup=get_skip_kb(),
        )
        await state.set_state(Registration.expectations)
    elif step_key == "informal_day":
        await _safe_answer(message,
            f"{p}{await _prompt('informal_day', 'Планируете ли вы посетить второй неформальный день (пройдёт загородом)?', participant_type)}",
            reply_markup=get_informal_day_kb(),
        )
        await state.set_state(Registration.informal_day)
    elif step_key == "attendance_format":
        await _safe_answer(message, f"{p}{await _prompt('attendance_format', 'В каком формате ты будешь присутствовать?', participant_type)}", reply_markup=get_attendance_format_kb())
        await state.set_state(Registration.attendance_format)
    elif step_key == "comments":
        await _safe_answer(message, f"{p}{await _prompt('comments', 'Любые вопросы/комментарии/пожелания:', participant_type)}", reply_markup=get_skip_kb())
        await state.set_state(Registration.comments)
    elif step_key == "department":
        await _safe_answer(message, f"{p}{await _prompt('department', 'Твой департамент:', participant_type)}", reply_markup=get_department_kb())
        await state.set_state(Registration.department)
    elif step_key == "aiesec_role":
        await _safe_answer(message, f"{p}{await _prompt('aiesec_role', 'Твоя позиция (Member/TL/Manager/VP/LCP/Coordinator):', participant_type)}", reply_markup=get_aiesec_role_kb())
        await state.set_state(Registration.aiesec_role)
    elif step_key == "needs_certificate":
        await _safe_answer(message, f"{p}{await _prompt('needs_certificate', 'Нужна справка в ВУЗ?', participant_type)}", reply_markup=get_yes_no_kb())
        await state.set_state(Registration.needs_certificate)
    elif step_key == "alumni_status":
        await _safe_answer(message,
            f"{p}{await _prompt('alumni_status', 'Ты аламни или айсекер?', participant_type)}",
            reply_markup=_reply_kb(["Аламни", "Айсекер", "Ни то, ни другое"]),
        )
        await state.set_state(Registration.alumni_status)
    elif step_key == "english_level":
        await _safe_answer(message, f"{p}{await _prompt('english_level', 'Уровень английского:', participant_type)}", reply_markup=get_english_level_kb())
        await state.set_state(Registration.english_level)
    elif step_key == "allergies":
        await _safe_answer(message, f"{p}{await _prompt('allergies', 'Есть ли у тебя аллергии на продукты/запахи? (если нет — поставь «-»)', participant_type)}", reply_markup=get_skip_kb())
        await state.set_state(Registration.allergies)
    elif step_key == "food_pref":
        await _safe_answer(message, f"{p}{await _prompt('food_pref', 'Особенности питания? Напиши, если ты веган/вегетарианец (иначе — обычное):', participant_type)}", reply_markup=get_skip_kb())
        await state.set_state(Registration.food_pref)
    elif step_key == "arrival":
        await _safe_answer(message, f"{p}{await _prompt('arrival', 'Когда приедешь?', participant_type)}", reply_markup=get_arrival_kb())
        await state.set_state(Registration.arrival)
    elif step_key == "housing":
        await _safe_answer(message, f"{p}{await _prompt('housing', 'Где будешь жить?', participant_type)}", reply_markup=get_housing_kb())
        await state.set_state(Registration.housing)
    elif step_key == "bed_sharing":
        await _safe_answer(message,
            f"{p}{await _prompt('bed_sharing', 'На площадке много двуспальных кроватей. Готов(а) спать с кем-то на одной кровати?', participant_type)}",
            reply_markup=_reply_kb(["Да", "Нет"]),
        )
        await state.set_state(Registration.bed_sharing)
    elif step_key == "bed_partner":
        await _safe_answer(message,
            f"{p}{await _prompt('bed_partner', 'С кем хотел(а) бы делить кровать? Напиши имя или «без разницы».', participant_type)}",
            reply_markup=get_skip_kb(),
        )
        await state.set_state(Registration.bed_partner)
    elif step_key == "transport":
        await _safe_answer(message,
            f"{p}{await _prompt('transport', 'Как добираешься до площадки?', participant_type)}",
            reply_markup=_reply_kb(["Трансфер до площадки", "Самостоятельно"]),
        )
        await state.set_state(Registration.transport)
    elif step_key == "payment_plan_date":
        # Deadline is pulled live from the payment_deadline setting (stored «ДД.ММ.ГГГГ ЧЧ:ММ»,
        # show just the date) — was hardcoded before, so it never synced with admin config.
        deadline = await get_setting("payment_deadline")
        dl_date = deadline.split()[0] if deadline else ""
        dl_note = f" Крайний срок: {dl_date}." if dl_date else ""
        default = f"Когда планируешь оплатить взнос?{dl_note} Введи дату (ДД.ММ.ГГГГ):"
        # Admin overrides may embed {deadline} to place the date wherever they want.
        prompt = (await _prompt('payment_plan_date', default, participant_type)).replace("{deadline}", dl_date)
        price_block = await _payment_price_block()  # show tariff prices before asking the date
        await _safe_answer(message, f"{p}{price_block}{prompt}", reply_markup=get_cancel_kb())
        await state.update_data(_current_date_step="payment_plan_date")
        await state.set_state(Registration.date_input)
    elif step_key == "cc_shop":
        await _safe_answer(message, f"{p}{await _prompt('cc_shop', 'Что бы ты хотел(а) видеть в CC-shop?', participant_type)}", reply_markup=get_skip_kb())
        await state.set_state(Registration.cc_shop)
    elif step_key == "exp_organizers":
        await _safe_answer(message, f"{p}{await _prompt('exp_organizers', 'Ожидания от команды организаторов?', participant_type)}", reply_markup=get_skip_kb())
        await state.set_state(Registration.exp_organizers)
    elif step_key == "exp_content":
        await _safe_answer(message, f"{p}{await _prompt('exp_content', 'Ожидания от контента?', participant_type)}", reply_markup=get_skip_kb())
        await state.set_state(Registration.exp_content)
    elif step_key == "volunteer":
        await _safe_answer(message, f"{p}{await _prompt('volunteer', 'Хочешь быть волонтёром?', participant_type)}", reply_markup=get_yes_no_kb())
        await state.set_state(Registration.volunteer)
    elif step_key == "resume":
        # Резюме обязательно (Таня п.2): без «Пропустить». Убираем reply-клаву целиком —
        # ответ = файл (PDF/DOCX) или текст, иначе шаг переспрашивает.
        await _safe_answer(message,
            f"{p}{await _prompt('resume', 'Прикрепи резюме файлом (PDF или DOCX) или напиши его текстом.', participant_type)}",
            reply_markup=ReplyKeyboardRemove(),
        )
        await state.set_state(Registration.resume)
    elif REG_STEP_TYPES.get(step_key) == "date":
        # Phase 4 (MOD-02): generic date-type step — one handler validates ДД.ММ.ГГГГ.
        label = REG_LABELS.get(f"reg_q_{step_key}", "Дата")
        await _safe_answer(message, f"{p}{await _prompt(step_key, f'{label} (ДД.ММ.ГГГГ):', participant_type)}", reply_markup=get_cancel_kb())
        await state.update_data(_current_date_step=step_key)
        await state.set_state(Registration.date_input)
    elif REG_STEP_TYPES.get(step_key) == "select":
        # Configurable single-select (study_field etc.) — options from settings.
        opt_key, default = SELECT_CONFIG.get(step_key, (f"{step_key}_options", []))
        options = await _get_options(opt_key, default)
        label = REG_LABELS.get(f"reg_q_{step_key}", "Выбери вариант")
        await _safe_answer(message, f"{p}{await _prompt(step_key, f'{label}:', participant_type)}", reply_markup=_reply_kb(options, add_other=True))
        await state.update_data(_current_select_step=step_key)
        await state.set_state(Registration.select_input)
    elif REG_STEP_TYPES.get(step_key) == "multi":
        # Configurable multi-select via inline toggle keyboard.
        opt_key, default = MULTI_CONFIG.get(step_key, (f"{step_key}_options", []))
        options = await _get_options(opt_key, default)
        label = REG_LABELS.get(f"reg_q_{step_key}", "Выбери варианты")
        await state.update_data(_current_multi_step=step_key, **{f"_multi_{step_key}": []})
        await _safe_answer(message,
            f"{p}{await _prompt(step_key, f'{label} (можно выбрать несколько):', participant_type)}",
            reply_markup=_multi_kb(step_key, options, set()),
        )
        await state.set_state(Registration.multi_input)
    elif step_key == "ambassador":
        await _safe_answer(message,
            f"{p}{await _prompt('ambassador', 'Хочешь стать амбассадором форума?', participant_type)}",
            reply_markup=_reply_kb(["Да!", "Пока нет"]),
        )
        await state.set_state(Registration.ambassador)
    elif step_key.startswith("consent:"):
        # Phase 4 (MOD-03, D-03/D-04): one consent per step, PDF attached if configured.
        consent_key = step_key.split(":", 1)[1]
        label = next(
            (lbl for lbl, k in await _consent_entries() if k == consent_key), consent_key
        )
        pdf_file_id = await get_setting(f"consent_pdf_{consent_key}")
        # Ссылки на документы уже в приветственном сообщении — показываем короткий вопрос.
        caption = html.escape(await _prompt(f'consent_{consent_key}', label, participant_type))
        btn_text = await get_setting("consent_button_text") or "Согласен(-на)"
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=btn_text, callback_data=f"consent_accept:{consent_key}")
        ]])
        if pdf_file_id:
            try:
                await message.answer_document(pdf_file_id, caption=caption, reply_markup=kb, parse_mode="HTML")
            except Exception:
                await message.answer_document(pdf_file_id, caption=caption, reply_markup=kb, parse_mode=None)
        else:
            try:
                await message.answer(caption, reply_markup=kb, parse_mode="HTML")
            except Exception:
                await message.answer(caption, reply_markup=kb, parse_mode=None)
        await state.update_data(_consent_key=consent_key)
        await state.set_state(Registration.consent_pending)


async def _advance(after_step: str, message: types.Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    enabled = await _get_enabled_steps(data)

    try:
        idx = enabled.index(after_step)
        next_idx = idx + 1
    except ValueError:
        # WR-01: the just-answered step is no longer in the enabled list (a question was
        # toggled off mid-flow, or a conditional removed it). Finalize instead of bouncing
        # the user back to step 0 — a silent restart is the wrong failure mode.
        next_idx = len(enabled)

    if next_idx < len(enabled):
        step = data.get("_reg_step", 0) + 1
        # WR-01: recompute total fresh each step. _reg_total was captured in process_full_name
        # before edu_conditional questions were unlocked, so a cached value produces bogus
        # progress numbers (e.g. "12/9"). Re-derive and re-persist from the live enabled count.
        total = len(enabled)
        await state.update_data(_reg_step=step, _reg_total=total)
        await _ask_step_or_recall(enabled[next_idx], message, state, step, total)
    else:
        # QW-01: show a summary + confirm keyboard before finalizing the full form (D-01).
        await _safe_answer(message, _build_summary(data), reply_markup=get_confirm_kb(), parse_mode="HTML")
        await state.set_state(Registration.confirm)


async def _ask_step_or_recall(step_key: str, message: types.Message, state: FSMContext, step: int, total: int):
    """Phase 07.3 (04, RET-02): the single seam in front of all 4 _ask_step call sites
    (RESEARCH Pattern 1). A returning delegate's non-empty prior answer for a recallable step
    is shown as a «Прошлый ответ ... Оставить/Изменить» screen instead of asking the question
    again; a newcomer (no _prior_answers), a step excluded from RECALLABLE_STEPS, or an empty
    prior value all fall straight through to the real _ask_step, byte-for-byte unchanged."""
    data = await state.get_data()
    prior = data.get("_prior_answers") or {}
    if prior and step_key == "resume":
        # Task 2 / Pitfall 3: resume gets its OWN carve-out, checked before the generic
        # RECALLABLE_STEPS gate ("resume" is deliberately excluded from that set) -- a raw
        # file_id/URL is meaningless to a human, so this screen never shows the prior value,
        # only the fact that one exists (T-073-04-02).
        has_prior_resume = any(
            prior.get(col) not in (None, "", "-")
            for col in ("resume_file_id", "resume_text", "resume_url")
        )
        if has_prior_resume:
            await _stamp_reg_step(step_key, message, data)
            p = await _progress(step, total)
            text = (
                f"{p}У нас есть твоё резюме с прошлой регистрации. "
                "Оставить его или прислать новое?"
            )
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="📎 Оставить прошлое резюме", callback_data="recall_keep:resume"),
                InlineKeyboardButton(text="📤 Загрузить новое", callback_data="recall_change:resume"),
            ]])
            await _safe_answer(message, text, reply_markup=kb)
            await state.update_data(_recall_step=step_key, _reg_step=step, _reg_total=total)
            await state.set_state(Registration.recall_pending)
            return
    if prior and step_key in RECALLABLE_STEPS:
        value = prior.get(STEP_TO_COLUMN[step_key])
        if value not in (None, "", "-"):
            await _stamp_reg_step(step_key, message, data)
            p = await _progress(step, total)
            label = dropout_step_label(step_key)
            display = _recall_display(step_key, value)
            text = f"{p}<b>{label}</b>\n\nПрошлый ответ: <b>{display}</b>\n\nОставить или изменить?"
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="✅ Оставить", callback_data=f"recall_keep:{step_key}"),
                InlineKeyboardButton(text="✏️ Изменить", callback_data=f"recall_change:{step_key}"),
            ]])
            await _safe_answer(message, text, reply_markup=kb, parse_mode="HTML")
            await state.update_data(_recall_step=step_key, _reg_step=step, _reg_total=total)
            await state.set_state(Registration.recall_pending)
            return
    await _ask_step(step_key, message, state, step, total)


@router.callback_query(F.data.startswith("recall_keep:"), Registration.recall_pending)
async def recall_keep(callback: types.CallbackQuery, state: FSMContext, bot: Bot):
    """T-073-04-01/Pitfall 2: add_user overwrites every non-COALESCE column unconditionally --
    "Оставить" MUST write the old value back into FSM data (except resume, a true no-op backed
    by add_user's own COALESCE), or the delegate's confirmed answer is silently lost."""
    step_key = callback.data.split(":", 1)[1]
    data = await state.get_data()
    # T-073-04-04: stale tap on a scrolled-up recall card -- same guard shape
    # process_consent_accept uses for the analogous consent-card problem.
    if not _consent_key_matches(step_key, data.get("_recall_step")):
        await callback.answer()
        return
    await callback.answer("✅ Оставили")
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    if step_key != "resume":
        column = STEP_TO_COLUMN[step_key]
        prior = data.get("_prior_answers") or {}
        value = prior.get(column)
        await state.update_data(**{column: value})
    # else: resume_file_id/resume_text/resume_url are left untouched in `data` -- add_user's
    # COALESCE (database/db.py) preserves whatever the prior row already had.
    tap_message = callback.message.model_copy(update={"from_user": callback.from_user})
    await _advance(step_key, tap_message, state, bot)


@router.callback_query(F.data.startswith("recall_change:"), Registration.recall_pending)
async def recall_change(callback: types.CallbackQuery, state: FSMContext):
    step_key = callback.data.split(":", 1)[1]
    data = await state.get_data()
    if not _consent_key_matches(step_key, data.get("_recall_step")):
        await callback.answer()
        return
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    tap_message = callback.message.model_copy(update={"from_user": callback.from_user})
    # Direct call to the REAL _ask_step, never the wrapper -- routing this through
    # _ask_step_or_recall would show the recall screen again for the very step the delegate
    # just chose to change, looping forever.
    await _ask_step(step_key, tap_message, state, data.get("_reg_step", 1), data.get("_reg_total", 1))


@router.message(Registration.recall_pending)
async def process_recall_ignore(message: types.Message):
    # Same shape as process_consent_ignore -- the recall screen is only advanced by a tap.
    await message.answer("Нажми «✅ Оставить» или «✏️ Изменить».")


# --- Helpers ---

def _extract_referrer_id(command_args: str | None, current_user_id: int) -> int | None:
    if not command_args:
        return None
    arg = command_args.strip()
    # CR-8: str.isdigit() accepts Unicode digits — some (²/①) crash int() with an unhandled
    # ValueError, others (fullwidth １２３) parse to a surprising value. Require plain ASCII
    # digits so only a normal Telegram id is accepted; anything else yields None, never a crash.
    if not (arg.isascii() and arg.isdigit()):
        return None
    referrer_id = int(arg)
    if referrer_id == current_user_id:
        return None
    return referrer_id


def _parse_age(raw: str | None) -> int | None:
    """CR-8: ASCII-digit-safe age parse. Returns the age for a plain 10..120 integer, else
    None (non-numeric, Unicode digits like ²/①, or out of range) — never raises."""
    raw = (raw or "").strip()
    if not (raw.isascii() and raw.isdigit()):
        return None
    age = int(raw)
    return age if 10 <= age <= 120 else None


def _consent_key_matches(tapped: str | None, active: str | None) -> bool:
    """CR-7: a consent tap is valid only for the currently-active consent step. A stale re-tap
    of an earlier card (scrolled up) has a key that no longer matches _consent_key → rejected."""
    return bool(active) and tapped == active


def _extract_source_tag(command_args: str | None) -> str | None:
    if not command_args:
        return None
    arg = command_args.strip()
    if arg.startswith("src_") and len(arg) > 4:
        return arg[4:]
    return None


# Phase 5 (TRACK-01/03): participant track vocabulary + deep-link parsing (D-10).
def _is_party_track(participant_type: str | None) -> bool:
    """The single predicate every later Phase 5 plan imports; do not duplicate elsewhere."""
    return participant_type in ("party_overnight", "party_noovernight")


# Phase 7 (SHORT-04): short-form track vocabulary.
SHORT_TRACK = "short"


def _is_short_track(participant_type: str | None) -> bool:
    """Exact-literal predicate for the short-form track — deliberately separate from
    _is_party_track, never merged into it. `_is_party_track` matches a CLOSED tuple of two
    literals; extending it to include "short" would leak the short track into the party-only
    housing/bed_* skip rule (:430), the party wording override in `_prompt`, and the
    party-first branch of `_decide_status` — all three gate on `_is_party_track` alone. Kept
    as its own one-line predicate so those three call sites structurally cannot see "short"."""
    return participant_type == SHORT_TRACK


async def _resolve_track(candidate: str | None, city_code: str | None = None) -> str:
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


# Fixed 2-entry map — exact-match only (T-05-01-01: no prefix/startswith/regex matching, so
# no crafted payload can produce a track value outside this closed vocabulary).
_PARTY_TAG_MAP = {"party_over": "party_overnight", "party_noover": "party_noovernight"}


def _extract_party_track(command_args: str | None) -> str | None:
    """D-10: matches ONLY the two literal party tokens, so a numeric arg still falls through
    to _extract_referrer_id and a "src_"-prefixed arg still falls through to
    _extract_source_tag — the three extractors are mutually exclusive by construction."""
    if not command_args:
        return None
    return _PARTY_TAG_MAP.get(command_args.strip())


# Phase 07.1 (CITY-01): fixed exact-match map, same construction as _PARTY_TAG_MAP — no
# startswith/regex, so a crafted deep-link payload can never select a city outside the
# codes currently known to the registry. Keys are "city_{code}"; these tokens do not
# overlap with the "src_" prefix, ASCII-digit referrer ids, or _PARTY_TAG_MAP's two
# literal tokens (proven by the mutual-exclusivity test matrix).
#
# Phase 14 (CITY-07, Pitfall 1): this used to be a module dict computed ONCE at import time
# from `CITIES` — a city added later from the bot got a deep-link token that never resolved,
# because the dict was never rebuilt on `reload_cities()`. It is now a function, recomputed
# from `all_cities()` on every call. `/start` happens far less often than the dict would be
# read, so paying the tiny recompute cost here removes the whole staleness class instead of
# wiring a callback from cities.py back into this module.
def _city_tag_map() -> dict[str, str]:
    return {f"city_{c['code']}": c["code"] for c in all_cities()}


def _extract_event_city(command_args: str | None) -> str | None:
    """Exact-match only — mirrors _extract_party_track's structure and mutual-exclusivity
    guarantee with _extract_referrer_id / _extract_source_tag / _extract_party_track."""
    if not command_args:
        return None
    return _city_tag_map().get(command_args.strip())


DEFAULT_PARTY_FORK_TEXT = "Выбери формат участия:"


def _party_fork_kb() -> InlineKeyboardMarkup:
    """D-10/D-09: pre-flow inline keyboard, not a REG_FLOW step. Reuses the same two literal
    party tokens as _PARTY_TAG_MAP so there is exactly one token vocabulary in this file."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Полная регистрация", callback_data="party_pick:full")],
        [InlineKeyboardButton(text="\U0001f389 Гости с ночёвкой", callback_data="party_pick:party_over")],
        [InlineKeyboardButton(text="\U0001f389 Гости без ночёвки", callback_data="party_pick:party_noover")],
    ])


async def _should_show_fork(party_track: str | None, recovered_track: str | None, is_registered: bool) -> bool:
    """D-10: pure(ish) gating helper for the pre-flow fork keyboard, extracted so it is
    testable without a Telegram update. ALL conditions below must hold before the fork is
    shown; when party_fork_question is at its default "off", this returns False for every
    combination of the other inputs (ROADMAP SC#5: an ordinary delegate sees zero extra
    screens). `party_track`/`recovered_track` truthy means an authoritative deep-link (or a
    recovered) value is already resolved — same posture as _source_from_tag (D-10)."""
    if party_track:
        return False
    if recovered_track:
        return False
    if is_registered:
        return False
    if await get_setting_typed("party_fork_question") != "on":  # REG-02: registry-backed
        return False
    # D-11a: offering a party option while the track is closed would contradict the master gate.
    if await get_setting_typed("party_enabled") != "on":  # REG-02: registry-backed
        return False
    return True


# Phase 07.1 (CITY-03): second pre-flow screen, modelled on _party_fork_kb /
# DEFAULT_PARTY_FORK_TEXT above. Order with the party fork is fixed and explicit in
# cmd_start (07.1-CONTEXT.md): party-closed gate -> welcome -> CITY -> party fork -> start.
DEFAULT_CITY_FORK_TEXT = "Выбери город мероприятия:"


async def _city_fork_kb() -> InlineKeyboardMarkup:
    """One button per ENABLED city, in CITIES (.env) order — label comes from city_label
    (admin-editable per-city override), callback_data is a closed `city_pick:{code}` token
    built from the registry, never from user input."""
    rows = []
    for c in await enabled_cities():
        rows.append([InlineKeyboardButton(text=await city_label(c["code"]), callback_data=f"city_pick:{c['code']}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# Phase 09.2-04 (CITY-04): проверено RESEARCH Pitfall 2 — гейт не зависит от
# registration_mode и не должен; заметка P2 «при краткой форме нет выбора города»
# воспроизводится только конфигурацией (event_city_enabled=off или <2 включённых городов),
# не кодовой зависимостью от режима формы. Поведение закреплено регрессионным тестом
# test_city_fork_shown_for_short_mode (tests/test_content_percity_consumers.py).
async def _should_show_city_fork(event_city: str | None, is_registered: bool) -> bool:
    """Pure(ish) gating helper for the city pre-flow screen, mirrors _should_show_fork.
    ALL conditions below must hold before the screen is shown; when event_city_enabled is
    "off" (default) this returns False for every combination (ROADMAP: zero extra screens
    for an ordinary delegate until a manager turns the module on)."""
    if event_city:
        return False  # city already authoritative — deep-link or recovered from reg_started
    if is_registered:
        return False
    if not await cities_module_on():
        return False
    enabled = await enabled_cities()
    if len(enabled) < 2:
        return False  # a single-button screen is pointless; the default resolves on read
    return True


async def _progress(step: int, total: int) -> str:
    """Optional «(3/9) » numbering prefix. Off by default (Tatiana: убрать нумерацию);
    organizers can switch it back on with reg_show_progress=on. Returns a trailing space
    so prompts read «{p}{question}» with no stray gap when disabled."""
    if await get_setting_typed("reg_show_progress") == "on":  # REG-02: registry-backed
        return f"({step}/{total}) "
    return ""


def _reply_kb(options: list[str], add_other: bool = False, add_skip: bool = False):
    """Build a one-time reply keyboard from a dynamic option list (no hardcoded buttons)."""
    kb = ReplyKeyboardBuilder()
    for opt in options:
        kb.button(text=opt)
    if add_other:
        kb.button(text="Другое")
    if add_skip:
        kb.button(text="Пропустить")
    kb.adjust(2)
    return kb.as_markup(resize_keyboard=True, one_time_keyboard=True)


def _multi_kb(step_key: str, options: list[str], selected: set[int]):
    """Inline toggle keyboard for a multi-select step. Each option toggles via
    regmulti:<step_key>:<idx>; «Готово» finalizes via regmulti_done:<step_key>."""
    rows = []
    for i, opt in enumerate(options):
        mark = "✅ " if i in selected else "▫️ "
        rows.append([InlineKeyboardButton(text=f"{mark}{opt}", callback_data=f"regmulti:{step_key}:{i}")])
    rows.append([InlineKeyboardButton(text="Готово", callback_data=f"regmulti_done:{step_key}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _sheet_details(data: dict) -> str:
    parts = []
    if data.get("referrer_id"):
        parts.append(f"Referrer ID: {data['referrer_id']}")
    return " | ".join(parts) if parts else "-"


# status код БД → человеческий ярлык в колонке «Статус» (Таня, п.5). «Новая» = ещё не
# смотрели (pending), «Одобрена»/«Отклонена» — после решения менеджера. Совпадает со
# списком значений выпадашки в services.sheets.STATUS_LABELS.
STATUS_LABELS = {"pending": "Новая", "approved": "Одобрена", "rejected": "Отклонена"}


def _status_label(data: dict) -> str:
    return STATUS_LABELS.get(data.get("status") or "pending", "Новая")


# Google Sheet columns: (header, gate_setting_or_None, value_fn). gate=None → always
# written (identity/system columns). gate=reg_q_* → column appears only when that question
# is enabled, so the sheet width tracks the active preset instead of always being 44 wide.
# Порядок = порядок вопросов в анкете (REG_FLOW), Таня п.1: сначала системные колонки
# (ID/Username/Дата/Статус/ФИО/Детали), затем вопросы ровно в том порядке, в каком их
# задаёт бот. При изменении порядка старые строки на листе разъедутся (шапка правится
# in place, данные — нет) — пользуйся admin «♻️ Пересобрать таблицу» для выравнивания.
SHEET_COLUMNS = [
    # --- системные (всегда) ---
    ("ID Telegram", None, lambda d: d.get("telegram_id") or "-"),
    ("Username", None, lambda d: d.get("username") or "-"),
    ("Дата регистрации", None, lambda d: d.get("registration_date") or "-"),
    ("Статус", None, _status_label),
    ("ФИО", None, lambda d: d.get("full_name") or "-"),
    ("Детали", None, _sheet_details),
    # --- вопросы в порядке REG_FLOW ---
    ("Телефон", "reg_q_phone", lambda d: d.get("phone") or "-"),
    ("Аламни/айсекер", "reg_q_alumni_status", lambda d: d.get("alumni_status") or "-"),
    ("ВК", "reg_q_vk", lambda d: d.get("vk_username") or "-"),
    ("Город", "reg_q_city", lambda d: d.get("city") or "-"),
    ("Образование", "reg_q_education", lambda d: d.get("education_status") or "-"),
    ("Курс", "reg_q_course", lambda d: d.get("course") or "-"),
    ("ВУЗ", "reg_q_university", lambda d: d.get("university") or "-"),
    ("Направление обучения", "reg_q_study_field", lambda d: d.get("study_field") or "-"),
    ("Цель участия", "reg_q_goal", lambda d: d.get("goal") or "-"),
    ("Форматы форума", "reg_q_formats", lambda d: d.get("formats") or "-"),
    ("Ожидания", "reg_q_expectations", lambda d: d.get("expectations") or "-"),
    ("Ожидания (AR)", "reg_q_expectations", lambda d: d.get("expectations_ar") or "-"),
    ("Источник", "reg_q_source", lambda d: d.get("source") or "-"),
    ("Амбассадор", "reg_q_ambassador", lambda d: "Да" if d.get("is_ambassador_candidate") else "-"),
    ("Резюме (текст)", "reg_q_resume", lambda d: d.get("resume_text") or "-"),
    ("Резюме (ссылка)", "reg_q_resume", lambda d: d.get("resume_url") or "-"),
    ("Email", "reg_q_email", lambda d: d.get("email") or "-"),
    ("Локальный комитет", "reg_q_lc", lambda d: d.get("local_committee") or "-"),
    ("Позиция", "reg_q_position", lambda d: d.get("position") or "-"),
    ("Специальность", "reg_q_specialty", lambda d: d.get("specialty") or "-"),
    ("Работает", "reg_q_work", lambda d: "Yes" if d.get("work_status") else "No"),
    ("Сфера работы", "reg_q_work_sphere", lambda d: d.get("work_sphere") or "-"),
    ("Не хватает навыков", "reg_q_skills", lambda d: d.get("missing_skills") or "-"),
    ("Формат участия", "reg_q_attendance", lambda d: d.get("attendance_format") or "-"),
    ("Неформальный день", "reg_q_informal_day", lambda d: d.get("informal_day") or "-"),
    ("Комментарии", "reg_q_comments", lambda d: d.get("comments") or "-"),
    ("Департамент", "reg_q_department", lambda d: d.get("department") or "-"),
    ("Роль AIESEC", "reg_q_aiesec_role", lambda d: d.get("aiesec_role") or "-"),
    ("Справка в ВУЗ", "reg_q_certificate", lambda d: d.get("needs_certificate") or "-"),
    ("Английский", "reg_q_english", lambda d: d.get("english_level") or "-"),
    ("Аллергии", "reg_q_allergies", lambda d: d.get("allergies") or "-"),
    ("Питание", "reg_q_food", lambda d: d.get("food_pref") or "-"),
    ("Приезд", "reg_q_arrival", lambda d: d.get("arrival") or "-"),
    ("Проживание", "reg_q_housing", lambda d: d.get("housing") or "-"),
    ("Общая кровать", "reg_q_bed_sharing", lambda d: d.get("bed_sharing") or "-"),
    ("Сосед по кровати", "reg_q_bed_partner", lambda d: d.get("bed_partner") or "-"),
    ("Трансфер", "reg_q_transport", lambda d: d.get("transport") or "-"),
    ("CC-shop", "reg_q_cc_shop", lambda d: d.get("cc_shop") or "-"),
    ("Ожидания от орг", "reg_q_exp_organizers", lambda d: d.get("exp_organizers") or "-"),
    ("Ожидания от контента", "reg_q_exp_content", lambda d: d.get("exp_content") or "-"),
    ("Волонтёр", "reg_q_volunteer", lambda d: d.get("volunteer") or "-"),
    ("Дата приезда", "reg_q_arrival_date", lambda d: d.get("arrival_date") or "-"),
    ("Дата рождения", "reg_q_birth_date", lambda d: d.get("birth_date") or "-"),
    ("Дата план. оплаты", "reg_q_payment_date", lambda d: d.get("payment_plan_date") or "-"),
]

# Full static header list (all columns) — kept for reference/tests. Live sync uses the
# dynamic active_sheet_headers() below.
SHEET_HEADERS = [h for h, _g, _f in SHEET_COLUMNS]


def _build_sheet_row(data: dict) -> list:
    """Full-width row (every column) — reference/tests. Live path uses active_sheet_row()."""
    return [fn(data) for _h, _g, fn in SHEET_COLUMNS]


def _sheet_value_map(data: dict) -> dict:
    return {h: fn(data) for h, _g, fn in SHEET_COLUMNS}


async def active_sheet_headers() -> list[str]:
    """Headers for only the columns whose gating question is enabled (system columns
    always included). The sheet width follows the active preset. NOTE: this reflects the
    CURRENT toggles — set the event type before delegates register (the physical header row
    is created once by ensure_sheet_header and is not rewritten if toggles change later)."""
    out = []
    for header, gate, _fn in SHEET_COLUMNS:
        if gate is None or await _is_step_enabled(gate):
            out.append(header)
    return out


async def set_sheet_schema(headers: list[str]) -> None:
    """CR-9: persist the header snapshot so appended rows stay aligned to the PHYSICAL header
    written to the sheet, even if question toggles change mid-event. Fail-soft — a bot_settings
    hiccup must never block startup or a rebuild."""
    try:
        await set_setting("sheet_header_schema", json.dumps(headers, ensure_ascii=False))
    except Exception:
        logger.warning("Failed to persist sheet_header_schema", exc_info=True)


async def get_sheet_schema() -> list[str]:
    """Frozen header snapshot (set at header write / rebuild). Falls back to the live
    active_sheet_headers() when no snapshot exists or it is malformed — zero migration risk
    for deployments that predate the snapshot."""
    raw = await get_setting("sheet_header_schema")
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list) and parsed:
                return [str(h) for h in parsed]
        except Exception:
            pass
    return await active_sheet_headers()


async def active_sheet_row(data: dict) -> list:
    """Row projected onto the FROZEN sheet schema (CR-9), aligned to the header actually on the
    sheet. Name-based projection; falls back to live headers when no snapshot is set.

    LOW (formula-injection parity, T-05-06-01): every projected cell is passed through
    database.db._csv_safe — the same neutralizer the party path already applies — so a crafted
    ФИО like «=HYPERLINK(...)» is stored as text on the MAIN tab too, not evaluated as a formula.
    Neutralizes only STRING cells starting with =,+,-,@,\\t,\\r (ints/None pass through)."""
    headers = await get_sheet_schema()
    values = _sheet_value_map(data)
    return [_csv_safe(values.get(h, "-")) for h in headers]


# Quick k4y: base identity/dropout columns for the «Незавершённые» tab, plus the set of
# system columns (from SHEET_COLUMNS) that make no sense for a not-yet-registered user
# (no registration date/status/referral details yet) and must be excluded when reusing
# active_sheet_headers().
INCOMPLETE_BASE_HEADERS = ["ID Telegram", "Username", "Начал регистрацию", "Остановился на"]
_INCOMPLETE_EXCLUDED_HEADERS = {"ID Telegram", "Username", "Дата регистрации", "Статус", "Детали"}


async def incomplete_sheet_headers() -> list[str]:
    """Headers for the «Незавершённые» tab: base dropout columns + ФИО + every currently
    enabled question column. Width follows the active preset, same as the main sheet
    (active_sheet_headers) — reuses it instead of duplicating the column list. Headers are
    computed ONCE per export/sync call, not per row (Google Sheets quota).

    Phase 7 (07-04, SHORT-06): during the promo window (or while an abandoned promo
    registration is still sitting in reg_started) a question can be enabled ONLY in the
    `__short` namespace while its global toggle stays off — active_sheet_headers() alone would
    then omit that column and a manager would see "-" instead of the delegate's actual answer.
    The merge branch below unions the global set with the __short set from SHEET_COLUMNS
    directly (order preserved, no duplicates).

    The gate is deliberately `registration_mode == "short" OR has_short_incomplete()`, NOT the
    live registration_mode alone: if only the live mode were checked, reverting the toggle to
    "full" on 2026-08-11 would make the very next 2h auto-sync (services/scheduler.py
    sync_incomplete_sheet_job) rewrite the tab with the narrow (non-merged) header set —
    sync_named_worksheet does a full clear+rewrite (services/sheets.py), so already-answered
    promo fields (Город, ВК, ...) would collapse to "-" even though reg_started.partial_data
    still holds them. has_short_incomplete() ties the merge to whether a live abandoned promo
    row still exists, not to a setting that has already been flipped back — once the last
    abandoned promo delegate finishes or is cleared (clear_reg_started), the tab narrows back
    on its own.

    Safe to widen/narrow on every call because sync_named_worksheet fully rewrites the tab
    (clear + write) rather than appending — unlike the main sheet, there are no previously
    written rows that could be misaligned by a header-width change.
    """
    if await get_setting_typed("registration_mode") == SHORT_TRACK or await has_short_incomplete():
        out = []
        for header, gate, _fn in SHEET_COLUMNS:
            if gate is None or await _is_step_enabled(gate) or await _is_step_enabled_for_track(gate, SHORT_TRACK):
                out.append(header)
        rest = [h for h in out if h not in _INCOMPLETE_EXCLUDED_HEADERS]
        return INCOMPLETE_BASE_HEADERS + rest
    active = await active_sheet_headers()
    rest = [h for h in active if h not in _INCOMPLETE_EXCLUDED_HEADERS]
    return INCOMPLETE_BASE_HEADERS + rest


def incomplete_sheet_row(
    telegram_id, username, started_at, last_step, partial_json, headers: list[str]
) -> list:
    """Row for the «Незавершённые» tab, projected onto `headers` (from
    incomplete_sheet_headers). Synchronous — every gate is already resolved in `headers`.
    `partial_json` is the FSM snapshot persisted by _ask_step; missing/None/malformed JSON
    degrades to an empty snapshot so every question cell renders "-" instead of raising."""
    try:
        parsed = json.loads(partial_json) if partial_json else {}
        if not isinstance(parsed, dict):
            parsed = {}
    except Exception:
        parsed = {}
    values = _sheet_value_map(parsed)
    values["ID Telegram"] = telegram_id
    values["Username"] = username or "-"
    values["Начал регистрацию"] = started_at or "-"
    values["Остановился на"] = dropout_step_label(last_step)
    return [_csv_safe(values.get(h, "-")) for h in headers]


# --- Phase 5 (D-11/D-12, TRACK-06): party worksheet tab -------------------------------------
# A deliberately curated column set, NOT a filtered copy of SHEET_COLUMNS: a party guest never
# has a university/course/study_field/resume, so those columns are omitted entirely rather than
# always appearing empty (the whole point of the separate tab).
PARTY_SHEET_COLUMNS = [
    ("ID Telegram", None, lambda d: d.get("telegram_id") or "-"),
    ("Username", None, lambda d: d.get("username") or "-"),
    ("Дата регистрации", None, lambda d: d.get("registration_date") or "-"),
    ("Статус", None, _status_label),
    ("ФИО", None, lambda d: d.get("full_name") or "-"),
    ("Трек", None, lambda d: {
        "party_overnight": "С ночёвкой", "party_noovernight": "Без ночёвки",
    }.get(d.get("participant_type"), "-")),
    ("Телефон", "reg_q_phone", lambda d: d.get("phone") or "-"),
    ("ВК", "reg_q_vk", lambda d: d.get("vk_username") or "-"),
    ("Аламни/айсекер", "reg_q_alumni_status", lambda d: d.get("alumni_status") or "-"),
    ("Аллергии", "reg_q_allergies", lambda d: d.get("allergies") or "-"),
    ("Питание", "reg_q_food", lambda d: d.get("food_pref") or "-"),
    ("Проживание", "reg_q_housing", lambda d: d.get("housing") or "-"),
    ("Общая кровать", "reg_q_bed_sharing", lambda d: d.get("bed_sharing") or "-"),
    ("Сосед по кровати", "reg_q_bed_partner", lambda d: d.get("bed_partner") or "-"),
]


async def party_sheet_headers() -> list[str]:
    """Mirror active_sheet_headers, but every gate is resolved through the tri-state
    __party override namespace (via the overnight sub-track) rather than the plain global
    gate — D-03 makes __party a single namespace shared by both sub-tracks, so resolving
    against "party_overnight" is correct; housing/bed columns simply stay empty for a
    no-overnight guest's row."""
    out = []
    for header, gate, _fn in PARTY_SHEET_COLUMNS:
        if gate is None or await _is_step_enabled_for_track(gate, "party_overnight"):
            out.append(header)
    return out


async def party_sheet_row(data: dict) -> list:
    """Mirror active_sheet_row. Claude's Discretion: no frozen-header snapshot for the party
    tab (unlike the main sheet's CR-9 sheet_header_schema) — party volume is low enough that
    live headers are acceptable; this is a deliberate scope choice, not an oversight.

    T-05-06-01: every registrant-supplied cell is passed through database.db._csv_safe (the
    reviewed formula-injection neutralizer, 260713-jgi) before being returned, matching the
    same mitigation used for the CSV export path. NOTE: the main sheet's active_sheet_row does
    NOT currently apply _csv_safe — that gap is out of this plan's scope (see SUMMARY)."""
    headers = await party_sheet_headers()
    values = {h: _csv_safe(fn(data)) for h, _g, fn in PARTY_SHEET_COLUMNS}
    return [values.get(h, "-") for h in headers]


PARTY_SHEET_TAB_DEFAULT = "Party"


async def append_to_party_sheet(data: list):
    """D-11: tab name resolved from the admin-configurable party_sheet_tab setting (added in
    plan 05-03) with a hardcoded fallback — same idiom as services/allowlist.py's DEFAULT_TAB."""
    tab = await get_setting("party_sheet_tab") or PARTY_SHEET_TAB_DEFAULT
    await append_to_named_sheet(tab, data)


# --- Phase 7 (SHORT-02, plan 07-02): short-track worksheet tab ------------------------------
# Deliberate divergence from RESEARCH.md's own recommendation ("courier columns, not a filter"):
# for party a curated list is mandatory because __party INHERITS the global question set — a
# plain filter over SHEET_COLUMNS would produce a ~30-column tab, half of it always empty. The
# short track (after 07-01) resolves an absent __short key to "not asked" (never inherits the
# global toggle), so filtering SHEET_COLUMNS by the short gate structurally yields EXACTLY the
# columns the short form can collect — nothing more. This is also the only way to keep the
# "configurable track" promise: a manager turns on a 7th `reg_q_*__short` question and the
# column appears on its own, no code change required.
SHORT_SHEET_SYSTEM_HEADERS = ["ID Telegram", "Username", "Дата регистрации", "Статус", "ФИО"]
# «Детали» (referrer) is intentionally excluded — the promo form never collects it.


async def short_sheet_headers() -> list[str]:
    """Filter over SHEET_COLUMNS (not a curated list, see module-level docstring above):
    system columns from SHORT_SHEET_SYSTEM_HEADERS always included, question columns included
    only when their `__short` gate resolves to enabled for SHORT_TRACK. Order follows
    SHEET_COLUMNS (== question order in the form) — never re-sorted."""
    out = []
    for header, gate, _fn in SHEET_COLUMNS:
        if gate is None:
            if header in SHORT_SHEET_SYSTEM_HEADERS:
                out.append(header)
        elif await _is_step_enabled_for_track(gate, SHORT_TRACK):
            out.append(header)
    return out


async def short_sheet_row(data: dict) -> list:
    """Mirror party_sheet_row: live headers (no frozen sheet_header_schema snapshot — promo
    volume is small, same deliberate scope choice as party). Every cell passes through
    database.db._csv_safe (T-07-04) so a crafted ФИО like «=HYPERLINK(...)» is stored as text,
    not evaluated as a formula."""
    headers = await short_sheet_headers()
    values = _sheet_value_map(data)
    return [_csv_safe(values.get(h, "-")) for h in headers]


SHORT_SHEET_TAB_DEFAULT = "Краткая"


async def append_to_short_sheet(data: list):
    """Same idiom as append_to_party_sheet: tab name resolved from the admin-configurable
    short_sheet_tab setting with a hardcoded fallback."""
    tab = await get_setting("short_sheet_tab") or SHORT_SHEET_TAB_DEFAULT
    await append_to_named_sheet(tab, data)


def _sheet_dispatch(participant_type: str | None) -> tuple:
    """(row_builder, appender) for the given track. Pure and synchronous on purpose — it
    resolves module-level functions by name at call time, which is what makes it testable
    without running the whole finalize_registration (Nextcloud upload, consent re-check,
    subscription persist, admin notify all live there and must NOT be dragged into this test).

    Exclusivity is a property of the function, not an optimization: exactly one (row, append)
    pair comes back for every input — there is nothing to combine, so a delegate's row can
    never land in more than one tab. Order of checks is load-bearing: party, then short, then
    the main sheet as the default. A double-write to the main tab would reproduce the exact
    header-mismatch problem (CONTEXT.md) this whole phase exists to remove."""
    if _is_party_track(participant_type):
        return party_sheet_row, append_to_party_sheet
    if _is_short_track(participant_type):
        return short_sheet_row, append_to_short_sheet
    return active_sheet_row, append_to_sheet


# --- Phase 07.1 (CITY-02, plan 07.1-02): city selects the TAB, track selects the COLUMNS ----
# _sheet_dispatch above stays the sole source of ROW BUILDER + exclusivity; the three helpers
# below only resolve a TAB NAME. They must never be merged into _sheet_dispatch's signature.
def _sheet_kind(participant_type: str | None) -> str:
    """Pure classification of a track into a tab "kind" for TAB_SUFFIX lookup. Mirrors
    _sheet_dispatch's own party -> short -> main check order (load-bearing, do not reorder)."""
    if _is_party_track(participant_type):
        return "party"
    if _is_short_track(participant_type):
        return "short"
    return "main"


async def city_row_tab(event_city: str | None, participant_type: str | None) -> str | None:
    """Tab name for a non-default city, or None meaning "use the legacy appender from
    _sheet_dispatch as-is" (Moscow / cities module off / no per-city tab-base override).

    The route deliberately does NOT depend on `is_city_enabled`: a registration submitted
    while a city was still enabled must keep landing on that city's own tab even if the city
    gets switched off afterwards (data integrity) — the enabled flag only affects the city-pick
    screen and startup tab materialization, never write routing."""
    if not await cities_module_on():
        return None
    code = normalize_city(event_city)
    if is_default_city(code):
        return None
    base = await city_tab_base(code)
    if not base:
        return None
    return f"{base}{await tab_suffix(_sheet_kind(participant_type))}"


async def city_incomplete_tab(event_city: str | None) -> str:
    """Always returns a tab name (never None) — the «Незавершённые» tab always has an
    address, unlike city_row_tab's legacy-appender escape hatch.

    Quick 260815-3hw: default tab name resolved from the registry (incomplete_sheet_tab,
    admin screen «📄 Вкладки таблицы»), same default "Незавершённые" as before."""
    default_tab = await get_setting_typed("incomplete_sheet_tab")
    if not await cities_module_on():
        return default_tab
    code = normalize_city(event_city)
    if is_default_city(code):
        return default_tab
    base = await city_tab_base(code)
    if not base:
        return default_tab
    return f"{base}{await tab_suffix('incomplete')}"


async def incomplete_city_batches() -> list[tuple[str, list[str], list[list]]]:
    """Single point of truth for BOTH the manual «Незавершённые → таблица» export
    (handlers/admin.py::export_incomplete) and the 2h auto-sync job
    (services/scheduler.py::sync_incomplete_sheet_job) — Phase 07.1 (CITY-04), extending the
    WR-01 parity guarantee to per-city tabs.

    Groups rows by the RESOLVED TAB NAME (city_incomplete_tab), not by raw city code: with the
    cities module off, every row's tab resolves to the single default «Незавершённые» name, so
    they all collapse into one batch — today's behavior, byte for byte. `headers` is computed
    exactly ONCE per call (Google Sheets quota) and shared by every batch.

    The default city's tab is always present, even with an empty row list, so a full
    clear+rewrite (sync_named_worksheet) keeps wiping out dropouts that have since registered
    or been cleared — the same guarantee get_incomplete_rows()-based callers relied on before
    this plan. Other cities' tabs are only included when they have at least one row — an empty
    non-default city tab is never created/wiped by this path."""
    rows = await get_incomplete_rows_with_city()
    headers = await incomplete_sheet_headers()
    default_tab = await city_incomplete_tab(None)
    batches: dict[str, list[list]] = {default_tab: []}
    for telegram_id, username, started_at, last_step, partial_data, event_city in rows:
        tab = await city_incomplete_tab(event_city)
        batches.setdefault(tab, [])
        batches[tab].append(
            incomplete_sheet_row(telegram_id, username, started_at, last_step, partial_data, headers)
        )
    return [(tab, headers, sheet_rows) for tab, sheet_rows in batches.items()]


def _esc(value) -> str:
    """Null-coalesce to '-' and HTML-escape free text for the summary message."""
    text = value if (value is not None and str(value) != "") else "-"
    return html.escape(str(text))


def _build_summary(data: dict) -> str:
    """QW-01 pre-finalize summary of the full-form answers (HTML, escaped)."""
    lines = ["<b>Проверь свои ответы:</b>", ""]
    fields = [
        ("ФИО", data.get("full_name")),
        ("Возраст", data.get("age")),
        ("Дата приезда", data.get("arrival_date")),
        ("Дата рождения", data.get("birth_date")),
        ("Email", data.get("email")),
        ("Телефон", data.get("phone")),
        ("ВК", data.get("vk_username")),
        ("Город", data.get("city")),
        ("Источник", data.get("source")),
        ("Лок. комитет", data.get("local_committee")),
        ("Позиция", data.get("position")),
        ("Образование", data.get("education_status")),
        ("ВУЗ", data.get("university")),
        ("Курс", data.get("course")),
        ("Специальность", data.get("specialty")),
        ("Направление обучения", data.get("study_field")),
        ("Работа", "Да" if data.get("work_status") else "Нет"),
        ("Сфера работы", data.get("work_sphere")),
        ("Навыки", data.get("missing_skills")),
        ("Ожидания", data.get("expectations")),
        ("Неформальный день", data.get("informal_day")),
        ("Формат", data.get("attendance_format")),
        ("Комментарии", data.get("comments")),
        ("Департамент", data.get("department")),
        ("Позиция AIESEC", data.get("aiesec_role")),
        ("Аламни/айсекер", data.get("alumni_status")),
        ("Справка в ВУЗ", data.get("needs_certificate")),
        ("Английский", data.get("english_level")),
        ("Аллергии", data.get("allergies")),
        ("Питание", data.get("food_pref")),
        ("Приезд", data.get("arrival")),
        ("Проживание", data.get("housing")),
        ("Общая кровать", data.get("bed_sharing")),
        ("Сосед по кровати", data.get("bed_partner")),
        ("Трансфер", data.get("transport")),
        ("Дата план. оплаты", data.get("payment_plan_date")),
        ("CC-shop", data.get("cc_shop")),
        ("Ожидания от орг", data.get("exp_organizers")),
        ("Ожидания от контента", data.get("exp_content")),
        ("Волонтёр", data.get("volunteer")),
        ("Цель участия", data.get("goal")),
        ("Форматы форума", data.get("formats")),
        ("Амбассадор", "Да" if data.get("is_ambassador_candidate") else None),
    ]
    for label, value in fields:
        if value is None or str(value) == "":
            continue
        lines.append(f"<b>{label}:</b> {_esc(value)}")
    if data.get("resume_file_id"):
        lines.append("<b>Резюме:</b> прикреплено файлом")
    elif data.get("resume_text"):
        lines.append(f"<b>Резюме:</b> {_esc(data.get('resume_text'))}")
    return "\n".join(lines)


def _is_allowed_resume(file_name: str | None) -> bool:
    """QW-03: accept only PDF/DOCX by extension (case-insensitive)."""
    if not file_name:
        return False
    name = file_name.lower()
    return name.endswith(".pdf") or name.endswith(".docx")


# P0 audit T-dw1-01: resume size guard, mirrors handlers.payment._RECEIPT_MAX_BYTES /
# _receipt_too_large. Replicated locally (not imported) to avoid a cross-module import risk
# and to keep the caps independently tunable.
_RESUME_MAX_BYTES = 10 * 1024 * 1024


def _resume_too_large(file_size) -> bool:
    return bool(file_size) and file_size > _RESUME_MAX_BYTES


# --- /start ---

async def _start_registration_flow(message: types.Message, state: FSMContext, referrer_id: int | None = None, source_tag: str | None = None, participant_type: str | None = None, event_city: str | None = None):
    existing_data = await state.get_data()
    # Phase 07.1 (CITY-03): same inherit-from-FSM idiom as saved_track below, deliberately
    # WITHOUT a normalize_city fallback — a delegate whose city was never chosen must stay
    # NULL in reg_started (07.1-CONTEXT.md: "Обратная совместимость"). Moscow is substituted
    # only on READ, in cities.normalize_city — never written here.
    # Phase 09.2-04 (CITY-04/RESEARCH Pitfall 3): computed BEFORE saved_track (moved up from
    # its original position below) so it can be passed into _resolve_track as a parameter —
    # the delegate's city is always already known by this point (07.1 fixes the screen order
    # welcome -> CITY -> track fork -> start), so re-deriving it a second time inside
    # _resolve_track would risk the two resolutions disagreeing.
    saved_city = event_city or existing_data.get("event_city")
    # Phase 5 (D-02): resolve the effective track BEFORE the mark_reg_started write — a fresh
    # deep-link arg wins; otherwise inherit whatever was already recorded in this FSM session
    # (mirrors saved_referrer_id / saved_source_tag one line below).
    # Phase 7: routed through _resolve_track so a global `registration_mode == "short"`
    # promo window can override an inherited "full"/None candidate (see _resolve_track
    # docstring). Note: `.get("participant_type")` has NO "full" default here on purpose —
    # _resolve_track's step 3 must see a real None to apply its own "full" fallback,
    # otherwise step 2 (the global short override) would never fire for a fresh session.
    saved_track = await _resolve_track(participant_type or existing_data.get("participant_type"), saved_city)

    # SCHED-02: record the dropout row at flow start (fail-soft — never block registration).
    try:
        await mark_reg_started(message.from_user.id, message.from_user.username, saved_track, saved_city)
    except Exception as e:
        logger.error(f"Failed to mark reg_started for {message.from_user.id}: {e}")

    saved_referrer_id = referrer_id or existing_data.get("referrer_id")
    saved_source_tag = source_tag or existing_data.get("source")
    # A src_ deep-link tag is authoritative: skip the «Источник» question so the delegate's
    # answer can't overwrite the campaign tag. Organic users (no tag) still get asked.
    source_from_tag = bool(source_tag) or existing_data.get("_source_from_tag", False)
    # Phase 5 (D-10, forward-compat only): mirror the _source_from_tag marker above. A fresh
    # `participant_type` arg means the track came from an authoritative source (deep link or
    # the reg_started recovery in cmd_start) this call. NOT consumed by any REG_FLOW step in
    # THIS phase — D-09 forbids adding one — recorded only so a later phase that does add a
    # fork REG_FLOW step key can express "skip it, the track is already authoritative" the
    # same way the existing source skip rule does.
    track_from_link = bool(participant_type) or existing_data.get("_track_from_link", False)
    # Phase 07.3 (RET-02): rereg_start snapshots the tapping delegate's OWN prior row into FSM
    # BEFORE calling this function (never inside it — T-073-03-02 keeps the row lookup pinned to
    # the tapper's own id). Same preserve-through-clear idiom as saved_referrer_id/saved_city
    # above/below — this is the only mechanism in this file for carrying a value across
    # state.clear(), no second one is invented.
    saved_prior = existing_data.get("_prior_answers")

    await state.clear()
    if saved_referrer_id:
        await state.update_data(referrer_id=saved_referrer_id)
        logger.info(f"Saved referrer_id={saved_referrer_id} for user {message.from_user.id}")
    if saved_source_tag:
        await state.update_data(source=saved_source_tag)
        logger.info(f"Saved source_tag={saved_source_tag} for user {message.from_user.id}")
        if source_from_tag:
            await state.update_data(_source_from_tag=True)
    # Phase 5 (D-02): persist the resolved track into FSM state so _get_enabled_steps and the
    # ask-step path can read it from data.
    await state.update_data(participant_type=saved_track)
    if track_from_link:
        await state.update_data(_track_from_link=True)
    # Phase 07.1 (CITY-03): only write event_city into FSM when one is actually known — an
    # unconditional write would invent a default in a session where no city was ever chosen.
    if saved_city:
        await state.update_data(event_city=saved_city)
    # Phase 07.3 (RET-02): re-apply the prior-answers snapshot AFTER clear, same position as
    # the other saved_x blocks above. T-073-03-03: the leading `_` keeps this key OUT of
    # _ask_step's «Незавершённые» snapshot (which filters `not k.startswith("_")`) — a past
    # season's personal data must never ride along into the incomplete-registrations sheet.
    if saved_prior:
        await state.update_data(_prior_answers=saved_prior)

    await message.answer(
        "Отлично, начинаем регистрацию."
        if not saved_referrer_id
        else "Отлично, ты пришёл по приглашению друга. Начинаем регистрацию."
    )
    # Tatiana: согласие — САМЫЙ первый шаг (перед ФИО). Run consents first; ФИО follows.
    consent_steps = await _get_consent_steps()
    if consent_steps:
        await state.update_data(_consent_queue=consent_steps, _consent_i=0)
        await _ask_step_or_recall(consent_steps[0], message, state, 1, len(consent_steps))
    else:
        await _ask_full_name(message, state)


async def _ask_full_name(message: types.Message, state: FSMContext):
    try:
        await set_reg_step(message.chat.id, "full_name")  # dropout analytics
    except Exception as e:
        logger.error(f"set_reg_step failed for {message.chat.id} @ full_name: {e}")
    await message.answer(await _prompt("full_name", "Напиши свои ФИО (Фамилия Имя Отчество):"), reply_markup=get_cancel_kb())
    await state.set_state(Registration.full_name)


async def _send_welcome(message: types.Message, text: str, photo_file_id: str | None, kb, user_id: int):
    short_text = len(text) <= 1024
    photo_sent = False

    photos_to_try = []
    if photo_file_id:
        photos_to_try.append(("DB", photo_file_id))
    if os.path.exists("resources/start.jpg"):
        photos_to_try.append(("file", FSInputFile("resources/start.jpg")))

    for src, photo in photos_to_try:
        if photo_sent:
            break
        if short_text:
            try:
                await message.answer_photo(photo, caption=text, reply_markup=kb, parse_mode="HTML")
                logger.info(f"Welcome: {src} photo + caption for {user_id}")
                return
            except Exception as e:
                logger.warning(f"Welcome: {src} photo+caption failed: {e}")
        try:
            await message.answer_photo(photo)
            photo_sent = True
            logger.info(f"Welcome: {src} photo (no caption) for {user_id}")
        except Exception as e:
            logger.warning(f"Welcome: {src} photo failed: {e}")

    try:
        await message.answer(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await message.answer(text, reply_markup=kb)
    logger.info(f"Welcome: text sent for {user_id}")


# --- QW-02 subscription check (observe-only, fail-open) ---

def _membership_status_to_bool(status: str) -> bool:
    """Map a Telegram chat-member status to subscribed/not. Restricted counts as not."""
    return status in ("creator", "administrator", "member")


def _normalize_channel_ref(raw: str | None) -> str | int | None:
    """HG-02: contact_tg is stored/displayed as a channel *link* (e.g. https://t.me/foo), but
    get_chat_member accepts only @username or a numeric chat id. Convert at check time WITHOUT
    mutating the stored value (it doubles as the user-facing contact link). Returns None for
    anything not resolvable by username (private invite links, joinchat/+hash, t.me/c/ paths,
    empty) so the caller skips the check and stays fail-open (D-07)."""
    if not raw:
        return None
    s = raw.strip()
    if s.lstrip("-").isdigit():  # numeric chat id like -1001234567890
        return int(s)
    for pref in ("https://", "http://"):
        if s.startswith(pref):
            s = s[len(pref):]
            break
    if s.startswith("t.me/"):
        s = s[len("t.me/"):]
    elif s.startswith("telegram.me/"):
        s = s[len("telegram.me/"):]
    s = s.split("?", 1)[0]  # drop any ?start=... query tail
    # Private invite links and multi-segment paths cannot be resolved to a public @username.
    if s.startswith("+") or "/" in s:
        return None
    s = s.lstrip("@")
    return ("@" + s) if s else None


async def is_subscribed(bot: Bot, channel, user_id: int) -> bool | None:
    """True/False membership; None on any error (bot not admin / unknown channel) — fail-open (D-07)."""
    try:
        member = await bot.get_chat_member(channel, user_id)
        return _membership_status_to_bool(member.status)
    except Exception as e:
        logger.warning(f"Subscription check failed for {user_id} on {channel!r}: {e}")
        return None


def _is_returning_row(user: dict | None, event_season: str | None) -> bool:
    """Phase 07.3 (RET-02/CONTEXT A): "прошлый делегат" = has a users row AND (status is
    'rejected' OR their row's `season` is set and differs from the currently configured
    `event_season`). Pure function (no await) — testable as a case table without a DB, and
    reusable as-is by `rereg_start`'s self-guard below (T-073-03-01).

    While `event_season` is unset (CONTEXT A: "фаза ведёт себя как сегодня"), only rejected
    rows are "returning" — parity with today's behaviour for every other row. A row with
    `season IS NULL` (pre-season legacy data) is never "returning" on this predicate alone;
    plan 02's "Новый сезон" button is what later stamps it with a season label.
    """
    if not user:
        return False
    status = user.get("status") or "approved"
    if status == "rejected":
        return True
    season = user.get("season")
    if event_season and season and season != event_season:
        return True
    return False


# bot is placed BEFORE the defaulted `command` param (a non-default arg after a default is a SyntaxError);
# aiogram injects by name so position is purely a syntax constraint.
@router.message(Command("start"), StateFilter("*"))
async def cmd_start(message: types.Message, state: FSMContext, bot: Bot, command: CommandObject | None = None):
    user_id = message.from_user.id
    logger.info(f"User {user_id} requested /start")

    # QW-02: observe-only subscription check — never blocks the user (D-04), never crashes /start (D-07).
    # HG-02: normalize the stored contact_tg link (t.me/foo) to a @username get_chat_member accepts.
    # HG-01: set_user_subscribed only persists for an EXISTING row; a first-touch registrant has no
    # row yet here, so finalize_registration re-runs this persist after add_user.
    try:
        channel = _normalize_channel_ref(await get_setting("contact_tg"))
        if channel is not None:
            result = await is_subscribed(bot, channel, user_id)
            if result is not None:
                await set_user_subscribed(user_id, result)
    except Exception as e:
        logger.warning(f"Subscription check skipped for {user_id}: {e}")

    # ME-05: an already-registered (non-rejected) user must always reach their normal
    # welcome/menu — the pre-selection gate below is an INTAKE filter for NEW registrants only.
    # Fetch the user row up front so toggling pre-selection ON can never lock an existing
    # approved delegate out of their own menu (e.g. they lost their @username, or the allowlist
    # was rebuilt without them). A rejected user still falls through to re-register (D-05a).
    user = await get_user(user_id)
    _already_registered = bool(user) and (user.get("status") or "approved") != "rejected"

    # VERIF-01/02: pre-selection gate (D-13, default off → live flow untouched).
    # Fail-soft like the subscription check above: a glitch never crashes /start.
    try:
        if not _already_registered and (await get_setting("preselect_enabled") or "off") == "on":
            from services.allowlist import is_allowed, allowlist_size, _parse_manual_ids
            if allowlist_size() == 0:
                # Owner-confirmed fail-open (Open Q2): admit everyone; the refresh job
                # alerts admins. Do NOT lock out a whole event over a sheet/quota glitch.
                logger.warning(f"Pre-selection ON but allowlist empty — fail-open admit for {user_id}")
            else:
                manual_ids = _parse_manual_ids(await get_setting("preselect_manual_ids"))
                uname = message.from_user.username
                if uname is None and user_id not in manual_ids:
                    prompt = await get_setting("preselect_no_username_text") or (
                        "Чтобы продолжить, задайте @username в настройках Telegram и снова отправьте /start."
                    )
                    await message.answer(html.escape(prompt))
                    return
                if uname is not None and not is_allowed(uname) and user_id not in manual_ids:
                    fail = await get_setting("preselect_fail_text") or "Отбор не пройден."
                    link = await get_setting("preselect_link")
                    text = html.escape(fail)
                    if link:
                        text += "\n" + html.escape(link)
                    await message.answer(text)
                    return
    except Exception as e:
        logger.warning(f"Pre-selection gate skipped for {user_id}: {e}")

    # user already fetched above (ME-05 gate bypass); do not re-query.
    args = command.args if command else None
    referrer_id = _extract_referrer_id(args, user_id)
    source_tag = _extract_source_tag(args)
    party_track = _extract_party_track(args)          # Phase 5 (D-10)
    dl_party_track = party_track  # preserved for the fork-suppression check below (D-10)
    dl_event_city = _extract_event_city(args)          # Phase 07.1 (CITY-03)
    if referrer_id:
        logger.info(f"Deep-link referrer_id={referrer_id} for user {user_id}")

    start_text = await get_setting("start_text") or DEFAULT_START_TEXT
    start_photo = await get_setting("start_photo_file_id")
    logger.info(f"Settings: start_text={start_text[:80]!r}, start_photo={start_photo!r}")

    # Phase 07.3 (RET-02): a "прошлый делегат" (rejected, or a PAST season's pending/approved
    # row) must never dead-end at /start — hand them the returning banner + rereg_start button
    # instead of falling into the already-registered branch below (which return()s without a
    # path forward) or silently dropping into a fresh registration (rejected's old behaviour).
    # Placed BEFORE the already-registered branch so it intercepts BOTH cases (CONTEXT B).
    # Fail-soft like every other lookup in this function: a resolve glitch degrades to "not
    # returning", never crashes /start (T-073-03-04).
    try:
        event_season = await get_setting("event_season")
        is_returning = _is_returning_row(user, event_season)
    except Exception as e:
        logger.error(f"returning-delegate predicate failed for {user_id}: {e}")
        is_returning = False
    if is_returning:
        prev_label = (user.get("season") or "").strip() or "прошлом событии"
        returning_text = await get_setting("start_text_returning") or DEFAULT_START_RETURNING_TEXT
        # T-073-03-05: str.replace, NOT .format() — an admin-authored text may contain other
        # unrelated {} that .format() would raise on.
        returning_text = returning_text.replace("{season}", prev_label)
        await _send_welcome(message, returning_text, start_photo, await get_main_menu_kb(user_id), user_id)
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="\U0001f680 Обновить анкету", callback_data="rereg_start")
        ]])
        await message.answer(
            "Хочешь участвовать снова? Обновим анкету — прошлые ответы предложу оставить.",
            reply_markup=kb,
        )
        # The tap on rereg_start arrives as a SEPARATE update, after this /start's local
        # variables (referrer_id/source_tag/party_track/dl_event_city) are gone — preserve
        # whatever this deep-link carried into FSM now, same idiom as the city-fork early
        # return below, so the campaign/referral/city attribution is not lost.
        if referrer_id:
            await state.update_data(referrer_id=referrer_id)
        if source_tag:
            await state.update_data(source=source_tag, _source_from_tag=True)
        if party_track:
            await state.update_data(participant_type=party_track, _track_from_link=True)
        if dl_event_city:
            await state.update_data(event_city=dl_event_city)
        return

    if user and (user.get("status") or "approved") != "rejected":
        # D-05a: a rejected user falls through to re-register; others see the welcome menu.
        # UAT 17.08: a returning delegate gets its OWN text, never the newcomer's start_text
        # (which reads "заявка займёт 5-7 минут…" and made testers think registration reset).
        logger.info(f"User {user_id} already registered")
        # Phase 09.2-04 (CITY-04/CONTEXT B): registered delegate's welcome-back text resolves
        # by their own event_city — reuse the `user` row already fetched above (ME-05), no
        # second get_user call. Each step is its own try/except: a city-resolve glitch must
        # never cost the delegate their welcome-back message (falls back to the global text).
        _registered_city = None
        try:
            if await cities_module_on():
                _registered_city = normalize_city(user.get("event_city"))
        except Exception as e:
            logger.error(f"per-city city resolve for start_text_registered failed for {user_id}: {e}")
            _registered_city = None
        try:
            registered_text = await get_setting_for_city("start_text_registered", _registered_city) or DEFAULT_START_REGISTERED_TEXT
        except Exception as e:
            logger.error(f"per-city start_text_registered resolve failed for {user_id}: {e}")
            registered_text = await get_setting("start_text_registered") or DEFAULT_START_REGISTERED_TEXT
        await _send_welcome(message, registered_text, start_photo, await get_main_menu_kb(user_id), user_id)

        if user_id in config.ADMIN_IDS:
            # quick-260817-4pj: inline button, NOT a second reply-keyboard + FSM state — a
            # second ReplyKeyboardMarkup here would overwrite the main menu just sent above,
            # and the old admin-rereg FSM state swallowed the admin's first tap on ANY menu button.
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="\U0001f504 Пройти регистрацию заново", callback_data="admin_rereg")
            ]])
            await message.answer(
                "Вы админ — можете пройти регистрацию заново для теста.",
                reply_markup=kb,
            )
        return

    # Phase 5 (D-11a): master toggle. Placed AFTER the already-registered branch above so it
    # fires ONLY for a user with no existing non-rejected users row — an already-registered
    # delegate tapping a stale/shared party link still gets their normal welcome + main menu
    # above, never this closed message. Same fail-soft posture as the pre-selection gate: a
    # gate bug must never crash /start.
    try:
        if party_track and await get_setting_typed("party_enabled") != "on":  # REG-02: registry-backed
            closed_text = await get_setting("party_closed_text") or (
                "Регистрация на вечеринку сейчас закрыта."
            )
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="Перейти к полной регистрации", callback_data="party_fallback_full")
            ]])
            await message.answer(closed_text, reply_markup=kb)
            return  # D-11a: NEVER silently reroute — user must tap the button to opt into `full`
    except Exception as e:
        logger.error(f"party_enabled gate failed for {user_id}: {e}")
        party_track = None  # fail-soft: never into a party track, never into a dead end

    # Phase 5 (D-02): a bare repeat /start (no deep-link arg) recovers the track from the
    # still-open reg_started row written by the first /start — only for a user who has no
    # users row yet (an already-registered user returned above and never reaches this line).
    recovered_track = None
    try:
        if not party_track and not user:
            recovered_track = await get_reg_started_track(user_id) or None
            party_track = recovered_track
    except Exception as e:
        logger.error(f"get_reg_started_track failed for {user_id}: {e}")

    # Phase 07.1 (CITY-03): same fail-soft recovery pattern as the track above — a bare
    # repeat /start (no city deep-link) recovers the city already chosen in this in-flight
    # registration, so the city screen is not shown twice.
    recovered_city = None
    try:
        if not dl_event_city and not user:
            recovered_city = await get_reg_started_city(user_id) or None
    except Exception as e:
        logger.error(f"get_reg_started_city failed for {user_id}: {e}")
    effective_city = dl_event_city or recovered_city

    # Phase 09.2-04 (CITY-04/CONTEXT A/B): before the city screen, an unknown city correctly
    # keeps the global start_text (that IS the right answer — CONTEXT A). Once the city is
    # already known (deep-link or recovered from an in-flight reg_started row), the newcomer
    # should see their own city's welcome text before ever reaching the city-fork screen.
    if effective_city:
        try:
            start_text = await get_setting_for_city("start_text", effective_city) or DEFAULT_START_TEXT
        except Exception as e:
            logger.error(f"per-city start_text resolve failed for {user_id}: {e}")

    logger.info(f"User {user_id} not registered, showing welcome then registration")
    await _send_welcome(message, start_text, start_photo, None, user_id)

    await _city_fork_then_continue(message, state, effective_city, referrer_id, source_tag, dl_party_track, recovered_track)


async def _city_fork_then_continue(
    message: types.Message,
    state: FSMContext,
    effective_city: str | None,
    referrer_id: int | None,
    source_tag: str | None,
    dl_party_track: str | None,
    recovered_track: str | None,
):
    """Phase 07.3 (RET-02): the former tail of cmd_start (city-fork screen or straight to
    _continue_after_city), extracted the same way 07.1 extracted _continue_after_city — so both
    a bare cmd_start AND rereg_start (a returning delegate's tap on "Обновить анкету") reach the
    SAME city/track-fork logic. `is_registered` stays hardcoded False in the _should_show_city_fork
    call below: a returning delegate is asked for city and track exactly like a newcomer
    (CONTEXT B: "переспрашиваем всегда"), never skipped just because they used to have one.

    `dl_party_track or recovered_track` below is algebraically the same value cmd_start's own
    local `party_track` held at this point pre-extraction (dl_party_track only ever diverges
    from party_track via the party-gate's fail-soft except-branch, which returns before reaching
    this tail) — kept as an expression rather than a third parameter so this function's
    signature stays exactly what plan 04 is told to expect.
    """
    user_id = message.from_user.id
    # Phase 07.1 (CITY-03): pre-flow city screen. Fixed order with the party fork
    # (07.1-CONTEXT.md): party-closed gate -> welcome -> CITY -> party fork -> start. We
    # reach this point only when there is no existing non-rejected users row (the
    # early-return above already handled that case), so `is_registered` is always False here.
    try:
        show_city = await _should_show_city_fork(effective_city, False)
    except Exception as e:
        logger.error(f"city fork gate check failed for {user_id}: {e}")
        show_city = False
    if show_city:
        # Same CR-01/HIGH-01 preserve idiom as the party fork below — persist attribution
        # (and the party track already known via deep-link/recovery) BEFORE the early return,
        # so city_pick's later call to _continue_after_city can round-trip it through FSM.
        _existing = await state.get_data()
        await state.update_data(
            referrer_id=referrer_id or _existing.get("referrer_id"),
            source=source_tag or _existing.get("source"),
            _source_from_tag=bool(source_tag) or bool(_existing.get("_source_from_tag")),
            participant_type=(dl_party_track or recovered_track) or _existing.get("participant_type"),
        )
        if dl_party_track:
            await state.update_data(_track_from_link=True)
        city_fork_text = await get_setting("city_fork_text") or DEFAULT_CITY_FORK_TEXT
        await message.answer(city_fork_text, reply_markup=await _city_fork_kb())
        return  # wait for the tap; city_pick continues the chain with the chosen city

    await _continue_after_city(message, state, effective_city, referrer_id, source_tag, dl_party_track, recovered_track)


async def _continue_after_city(
    message: types.Message,
    state: FSMContext,
    event_city: str | None,
    referrer_id: int | None,
    source_tag: str | None,
    dl_party_track: str | None,
    recovered_track: str | None,
):
    """Phase 07.1 (CITY-03): the former tail of cmd_start (party fork gate + flow start),
    extracted so both a bare cmd_start (city screen skipped/already resolved) and city_pick
    (after a tap) reach the SAME party-fork-then-start logic. Body is byte-identical to the
    pre-07.1-03 tail except for the two event_city additions marked below — city travels
    through party_pick the same way participant_type already does, by round-tripping through
    FSM state before any early return."""
    user_id = message.from_user.id
    party_track = dl_party_track or recovered_track

    # Phase 5 (D-10): optional pre-flow fork question, behind party_fork_question (default
    # off). We reach this point only when there is no existing non-rejected users row (the
    # early-return above already handled that case), so `is_registered` is always False here.
    try:
        show_fork = await _should_show_fork(dl_party_track, recovered_track, False)
    except Exception as e:
        logger.error(f"fork gate check failed for {user_id}: {e}")
        show_fork = False
    if show_fork:
        # CR-01 fix: persist the deep-link attribution BEFORE the fork gate returns, using the
        # exact same FSM keys _start_registration_flow reads back via existing_data.get(...)
        # (referrer_id / source / _source_from_tag). Without this, party_pick and
        # party_fallback_full call _start_registration_flow with an empty FSM state (this is
        # the first call for the session) and referrer_id/source_tag are silently dropped for
        # EVERY user who lands on the fork screen — including one who picks "full".
        # HIGH-01: preserve prior attribution. A referred user re-sending a BARE /start
        # (referrer_id=None) while the fork keyboard is shown must not clobber the referrer
        # saved by their first (deep-linked) /start. Same preserve idiom _start_registration_flow
        # uses: fall back to whatever is already in FSM state.
        _existing = await state.get_data()
        await state.update_data(
            referrer_id=referrer_id or _existing.get("referrer_id"),
            source=source_tag or _existing.get("source"),
            _source_from_tag=bool(source_tag) or bool(_existing.get("_source_from_tag")),
            # Phase 07.1 (CITY-03): carry the already-resolved city through the party fork too
            # — party_pick calls _start_registration_flow WITHOUT a city, so it must be able to
            # read it back from FSM the same way it already does for participant_type. Mirror
            # the same preserve idiom for participant_type itself (this is a no-op whenever
            # dl_party_track/recovered_track are set, since _should_show_fork already returns
            # False in that case — kept for symmetry so a future caller of this branch can't
            # silently drop an already-known track the way pre-fix referrer_id/source did).
            event_city=event_city,
            participant_type=dl_party_track or recovered_track or _existing.get("participant_type"),
        )
        fork_text = await get_setting("party_fork_text") or DEFAULT_PARTY_FORK_TEXT
        await message.answer(fork_text, reply_markup=_party_fork_kb())
        return  # wait for the tap; the party_pick handler starts the flow with the chosen track

    await _start_registration_flow(
        message, state, referrer_id=referrer_id, source_tag=source_tag,
        participant_type=party_track, event_city=event_city,
    )


@router.callback_query(F.data.startswith("party_pick:"))
async def party_pick(callback: types.CallbackQuery, state: FSMContext):
    """D-10 fork tap. The token is mapped through the SAME _PARTY_TAG_MAP the deep-link
    extractor uses (T-05-04-01: one closed token vocabulary, so no arbitrary track value can
    reach _start_registration_flow) — an unmapped token is rejected and answered, not routed
    anywhere. The party_enabled gate is re-checked here since the setting can flip between
    the fork being rendered and the user tapping it."""
    token = callback.data.split(":", 1)[1]
    if token == "full":
        chosen_track = None
    elif token in _PARTY_TAG_MAP:
        chosen_track = _PARTY_TAG_MAP[token]
    else:
        await callback.answer("Некорректный выбор.", show_alert=True)
        return

    if chosen_track and await get_setting_typed("party_enabled") != "on":  # REG-02: registry-backed
        # Render-then-flip window (T-05-04-01): the track closed between render and tap.
        await callback.answer("Регистрация на вечеринку уже закрыта.", show_alert=True)
        return

    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    # callback.message.from_user is the BOT — swap in the tapping user, same fix as
    # party_fallback_full (T-05-01 deviation), so mark_reg_started records the right id.
    tap_message = callback.message.model_copy(update={"from_user": callback.from_user})
    await _start_registration_flow(tap_message, state, participant_type=chosen_track)


@router.callback_query(F.data == "admin_rereg")
async def admin_rereg(callback: types.CallbackQuery, state: FSMContext):
    """quick-260817-4pj: admin-only re-registration entry point, replaces the old
    admin-rereg reply-keyboard FSM flow. registration.router is NOT covered by
    CapabilityMiddleware (it only sits on admin.router), so the ADMIN_IDS check below is the
    sole guard — anyone who guessed this callback_data could otherwise reset their own FSM and
    start a registration (T-4pj-01)."""
    if callback.from_user.id not in config.ADMIN_IDS:
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    await callback.answer()
    await state.clear()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    # callback.message.from_user is the BOT — swap in the tapping admin, same fix as
    # party_fallback_full, so mark_reg_started records the tapping admin's id (T-4pj-02).
    tap_message = callback.message.model_copy(update={"from_user": callback.from_user})
    await _start_registration_flow(tap_message, state)


@router.callback_query(F.data == "rereg_start")
async def rereg_start(callback: types.CallbackQuery, state: FSMContext):
    """Phase 07.3 (RET-02/T-073-03-01): registration.router is NOT covered by
    CapabilityMiddleware (only admin.router is) — same posture as admin_rereg above. Anyone who
    guessed this callback_data could otherwise reset a returning delegate's own FSM without
    actually being a returning delegate, so this handler re-verifies the TAPPER (never anything
    from the callback payload) against `_is_returning_row` before doing anything else."""
    user = await get_user(callback.from_user.id)
    event_season = await get_setting("event_season")
    if not _is_returning_row(user, event_season):
        await callback.answer("Ты уже зарегистрирован(а) на этот сезон", show_alert=True)
        return
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    # T-073-03-02: the snapshot is taken ONLY from the tapping user's OWN row, fetched above by
    # callback.from_user.id — never from any callback/deep-link parameter, so a crafted
    # callback_data can never pull someone else's prior answers into this session.
    await state.update_data(_prior_answers=dict(user))
    # callback.message.from_user is the BOT, not the tapping delegate — same fix as admin_rereg
    # above (T-4pj-02), so mark_reg_started/attribution downstream records the tapper's own id.
    tap_message = callback.message.model_copy(update={"from_user": callback.from_user})
    data = await state.get_data()
    await _city_fork_then_continue(
        tap_message,
        state,
        data.get("event_city"),
        data.get("referrer_id"),
        data.get("source"),
        data.get("participant_type") if data.get("_track_from_link") else None,
        None,
    )


@router.callback_query(F.data == "party_fallback_full")
async def party_fallback_full(callback: types.CallbackQuery, state: FSMContext):
    """D-11a explicit opt-in: the ONLY way out of the "party closed" message. Starts the
    ordinary full flow (participant_type left at its default 'full') — never sets a party
    track, carries no user-supplied parameters (T-05-01-03)."""
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    # callback.message.from_user is the BOT (it authored that message) — swap in the actual
    # tapping user (callback.from_user) so mark_reg_started records the right telegram_id.
    # model_copy() preserves the bound _bot private attr, so .answer() still works.
    tap_message = callback.message.model_copy(update={"from_user": callback.from_user})
    await _start_registration_flow(tap_message, state)


@router.callback_query(F.data.startswith("city_pick:"))
async def city_pick(callback: types.CallbackQuery, state: FSMContext):
    """CITY-03 city-screen tap. The code is checked against the CLOSED CITIES vocabulary
    (T-071-09: no crafted callback payload can select a city outside the registry) before any
    action; is_city_enabled is re-checked AFTER render (T-071-10, render-then-flip window —
    same pattern as party_pick's party_enabled re-check). Continues through the SAME
    _continue_after_city tail cmd_start uses, so the party fork still fires if applicable and
    referrer/source attribution (persisted into FSM by cmd_start before showing this screen)
    is picked back up there, not passed again here."""
    code = callback.data.split(":", 1)[1]
    if code not in {c["code"] for c in CITIES}:
        await callback.answer("Некорректный выбор.", show_alert=True)
        return

    if not await is_city_enabled(code):
        await callback.answer("Регистрация на этот город закрыта.", show_alert=True)
        return

    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    # callback.message.from_user is the BOT — swap in the tapping user, same fix as party_pick.
    tap_message = callback.message.model_copy(update={"from_user": callback.from_user})
    data = await state.get_data()
    pending_track = data.get("participant_type")
    await _continue_after_city(tap_message, state, code, None, None, pending_track, None)


_CANCEL_CONFIRM_KB = InlineKeyboardMarkup(inline_keyboard=[[
    InlineKeyboardButton(text="Да, отменить", callback_data="reg_cancel_yes"),
    InlineKeyboardButton(text="Нет, продолжить", callback_data="reg_cancel_no"),
]])


@router.message(StateFilter(Registration), F.text.in_({"Отмена", "/cancel"}))
async def cancel_registration(message: types.Message, state: FSMContext):
    # Confirm before wiping the form — one accidental tap on the «Отмена» reply
    # button used to drop the whole registration with no undo. State is left intact
    # so «Нет, продолжить» resumes exactly where the user was.
    await message.answer(
        "Точно отменить регистрацию? Все введённые ответы сотрутся.",
        reply_markup=_CANCEL_CONFIRM_KB,
    )


@router.callback_query(F.data == "reg_cancel_yes", StateFilter(Registration))
async def cancel_registration_confirm(callback: types.CallbackQuery, state: FSMContext):
    # WR-05: state-scoped so a stale "Точно отменить?" button can only clear an active
    # registration — never wipe unrelated FSM state (e.g. an admin mid-broadcast/settings).
    await state.clear()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.message.answer(
        "Регистрация отменена. Чтобы начать заново, отправь /start.",
        reply_markup=ReplyKeyboardRemove(),
    )
    await callback.answer()


@router.callback_query(F.data == "reg_cancel_no", StateFilter(Registration))
async def cancel_registration_dismiss(callback: types.CallbackQuery):
    # Keep the FSM state untouched — the current step's question is still above, so
    # the user just carries on answering it.
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.answer("Продолжаем 👍")


# --- QW-01 confirmation step ---

@router.message(Registration.confirm, F.text == "Всё верно")
async def process_confirm_ok(message: types.Message, state: FSMContext, bot: Bot):
    await finalize_registration(message, state, bot)


@router.message(Registration.confirm, F.text == "Изменить")
async def process_confirm_edit(message: types.Message, state: FSMContext):
    # D-02: 'Изменить' restarts the whole flow — no per-field editing.
    await _start_registration_flow(message, state)


# --- QW-03 resume upload step ---

@router.message(Registration.resume, F.document)
async def process_resume(message: types.Message, state: FSMContext, bot: Bot):
    if not _is_allowed_resume(message.document.file_name):
        await message.answer("Принимаются только PDF или DOCX. Прикрепи файл ещё раз.")
        return
    if _resume_too_large(message.document.file_size):
        await message.answer("❌ Файл слишком большой (максимум 10 МБ). Прикрепи резюме меньшего размера.")
        return
    # file_id only, no download (D-10). Keep the original file name in FSM (non-persisted,
    # no DB/sheet column) so the Nextcloud upload preserves the real .pdf/.docx extension.
    await state.update_data(
        resume_file_id=message.document.file_id,
        resume_file_name=message.document.file_name,
    )
    await _advance("resume", message, state, bot)


@router.message(Registration.resume, F.text)
async def process_resume_text(message: types.Message, state: FSMContext, bot: Bot):
    # Tatiana: резюме можно либо файлом, либо текстом. Обязательно (без «Пропустить»).
    text = (message.text or "").strip()
    if not text:
        await message.answer("Напиши резюме текстом или прикрепи файл (PDF или DOCX).")
        return
    await state.update_data(resume_text=text)
    await _advance("resume", message, state, bot)


@router.message(Registration.resume)
async def process_resume_invalid(message: types.Message, state: FSMContext):
    # Neither a document nor text (sticker/photo/etc.) — re-prompt, never crash (D-10).
    await message.answer("Пришли резюме текстом или прикрепи файл (PDF или DOCX).")


# --- Phase 4: date-type step (MOD-02) ---

def _validate_date_range(step_key: str, dt: datetime) -> str | None:
    """LOW: sanity range check for a parsed date step. Loose bounds — reject only clearly-wrong
    input (typos, impossible dates), never a plausible real value. Returns a user-facing error
    message, or None if the date is acceptable."""
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


@router.message(Registration.date_input)
async def process_date_input(message: types.Message, state: FSMContext, bot: Bot):
    raw = (message.text or "").strip()
    try:
        dt = datetime.strptime(raw, "%d.%m.%Y")
    except ValueError:
        await message.answer("Формат даты: ДД.ММ.ГГГГ. Попробуй ещё раз.")
        return
    data = await state.get_data()
    step_key = data.get("_current_date_step", "arrival_date")
    range_err = _validate_date_range(step_key, dt)
    if range_err:
        await message.answer(range_err)
        return
    await state.update_data(**{step_key: raw})
    await _advance(step_key, message, state, bot)


# --- YL'26: configurable single-select step ---

@router.message(Registration.select_input)
async def process_select_input(message: types.Message, state: FSMContext, bot: Bot):
    text = (message.text or "").strip()
    if not text:
        await message.answer("Выбери вариант на клавиатуре или напиши свой.")
        return
    if text == "Другое":
        await message.answer("Напиши свой вариант:", reply_markup=get_cancel_kb())
        return
    data = await state.get_data()
    step_key = data.get("_current_select_step", "study_field")
    await state.update_data(**{step_key: text})
    await _advance(step_key, message, state, bot)


# --- YL'26: configurable multi-select step (inline toggles) ---

async def _multi_options(step_key: str) -> list[str]:
    opt_key, default = MULTI_CONFIG.get(step_key, (f"{step_key}_options", []))
    return await _get_options(opt_key, default)


@router.callback_query(F.data.startswith("regmulti:"), Registration.multi_input)
async def process_multi_toggle(callback: types.CallbackQuery, state: FSMContext):
    _, step_key, idx_raw = callback.data.split(":", 2)
    try:
        idx = int(idx_raw)
    except ValueError:
        await callback.answer()
        return
    data = await state.get_data()
    selected = set(data.get(f"_multi_{step_key}", []))
    if idx in selected:
        selected.discard(idx)
    else:
        selected.add(idx)
    await state.update_data(**{f"_multi_{step_key}": sorted(selected)})
    options = await _multi_options(step_key)
    try:
        await callback.message.edit_reply_markup(reply_markup=_multi_kb(step_key, options, selected))
    except Exception:
        pass
    await callback.answer()


@router.callback_query(F.data.startswith("regmulti_done:"), Registration.multi_input)
async def process_multi_done(callback: types.CallbackQuery, state: FSMContext, bot: Bot):
    step_key = callback.data.split(":", 1)[1]
    data = await state.get_data()
    selected = sorted(set(data.get(f"_multi_{step_key}", [])))
    options = await _multi_options(step_key)
    chosen = [options[i] for i in selected if 0 <= i < len(options)]
    if not chosen:
        await callback.answer("Выбери хотя бы один вариант.", show_alert=True)
        return
    await state.update_data(**{step_key: ", ".join(chosen)})
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.answer("Сохранено")
    await _advance(step_key, callback.message, state, bot)


@router.message(Registration.multi_input)
async def process_multi_ignore(message: types.Message):
    await message.answer("Отмечай варианты кнопками выше и нажми «Готово».")


# --- YL'26: ambassador yes/no ---

@router.message(Registration.ambassador)
async def process_ambassador(message: types.Message, state: FSMContext, bot: Bot):
    text = (message.text or "").strip()
    if not text:
        await message.answer("Выбери «Да!» или «Пока нет».")
        return
    await state.update_data(is_ambassador_candidate=text.lower().startswith("да"))
    await _advance("ambassador", message, state, bot)


# --- Phase 4: consent steps (MOD-03, CONS-02) ---

@router.callback_query(F.data.startswith("consent_accept:"), Registration.consent_pending)
async def process_consent_accept(callback: types.CallbackQuery, state: FSMContext, bot: Bot):
    consent_key = callback.data.split(":", 1)[1]
    data = await state.get_data()
    # CR-7: only the currently-active consent may advance the flow. A stale re-tap of an
    # earlier consent card (user scrolled up) carries an old key → ignore silently, record
    # nothing, do not advance. This guarantees every required consent is accepted in order.
    if not _consent_key_matches(consent_key, data.get("_consent_key")):
        await callback.answer()
        return
    await record_user_consent(callback.from_user.id, consent_key)  # D-02 audit row
    await callback.answer("✅ Принято")
    # Defense-in-depth: disable the tapped card's button so it can't be re-used.
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    # Consents run before ФИО: walk the consent queue, then ask ФИО.
    queue = data.get("_consent_queue", [])
    i = data.get("_consent_i", 0) + 1
    if i < len(queue):
        await state.update_data(_consent_i=i)
        await _ask_step_or_recall(queue[i], callback.message, state, i + 1, len(queue))
    else:
        await _ask_full_name(callback.message, state)


@router.message(Registration.consent_pending)
async def process_consent_ignore(message: types.Message):
    # SC#2: consent cannot be skipped via text — only the consent button advances.
    btn_text = await get_setting("consent_button_text") or "Согласен(-на)"
    await message.answer(f"Нажми кнопку «{btn_text}» для продолжения.")



# --- Core Registration ---

@router.message(Registration.full_name)
async def process_full_name(message: types.Message, state: FSMContext, bot: Bot):
    full_name = (message.text or "").strip()
    if len(full_name.split()) < 2:
        await message.answer("Укажи ФИО полностью (минимум фамилию и имя).")
        return

    await state.update_data(full_name=full_name)

    # Phase 7 (SHORT-04): the old `registration_mode != "full"` early-exit straight to
    # finalize() is REMOVED. The generic engine below now runs for every track, including
    # "short" — `_get_enabled_steps` (Task 1) resolves the short track's own `reg_q_*__short`
    # keys, and with zero such keys set it returns [] exactly like today, so the
    # "no steps -> finalize" branch four lines down still fires and reproduces the historical
    # short-form behavior STRUCTURALLY, not via a duplicated special case. Two consequences
    # that are NOT regressions:
    # 1. A short form with >=1 enabled question now reaches _advance and shows the
    #    _build_summary confirmation screen before finalizing, same as the full form — this
    #    is the whole point of the phase (a real multi-question track, not just ФИО).
    # 2. `registration_mode` is no longer read here at all. The only place the live mode
    #    setting affects the question set is `_resolve_track` at flow start (Task 1c) — a
    #    delegate who already started a track keeps finishing in that track even if the
    #    manager flips the toggle mid-session.
    data = await state.get_data()
    enabled = await _get_enabled_steps(data)

    if not enabled:
        await finalize_registration(message, state, bot)
        return

    total = len(enabled)
    await state.update_data(_reg_step=1, _reg_total=total)
    await _ask_step_or_recall(enabled[0], message, state, 1, total)


# --- Extended Question Handlers ---

@router.message(Registration.age)
async def process_age(message: types.Message, state: FSMContext, bot: Bot):
    age = _parse_age(message.text)
    if age is None:
        await message.answer("Укажи корректный возраст числом от 10 до 120.")
        return
    await state.update_data(age=age)
    await _advance("age", message, state, bot)


@router.message(Registration.email)
async def process_email(message: types.Message, state: FSMContext, bot: Bot):
    email = (message.text or "").strip()
    if not email or "@" not in email or "." not in email:
        await message.answer("Укажи корректный email (например, name@example.com).")
        return
    await state.update_data(email=email)
    await _advance("email", message, state, bot)


@router.message(Registration.phone, F.contact)
async def process_phone_contact(message: types.Message, state: FSMContext, bot: Bot):
    phone = message.contact.phone_number
    if not phone.startswith("+"):
        phone = f"+{phone}"
    await state.update_data(phone=phone)
    await _advance("phone", message, state, bot)


@router.message(Registration.phone)
async def process_phone(message: types.Message, state: FSMContext, bot: Bot):
    text = (message.text or "").strip()
    if text == "Пропустить":
        await state.update_data(phone="-")
        await _advance("phone", message, state, bot)
        return
    cleaned = text.replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
    if not cleaned:
        await message.answer("Укажи номер телефона или нажми «Пропустить».")
        return
    if not (cleaned.startswith("+") and cleaned[1:].isdigit()) and not cleaned.isdigit():
        await message.answer("Укажи корректный номер телефона или нажми «Пропустить».")
        return
    await state.update_data(phone=text)
    await _advance("phone", message, state, bot)


@router.message(Registration.vk)
async def process_vk(message: types.Message, state: FSMContext, bot: Bot):
    vk = (message.text or "").strip()
    # Tatiana: ник строго в формате @username.
    if not vk.startswith("@") or len(vk) < 2 or " " in vk:
        await message.answer("Укажи ник в ВК в формате @username (начинается с @, без пробелов).")
        return
    await state.update_data(vk_username=vk)
    await _advance("vk", message, state, bot)


@router.message(Registration.city)
async def process_city(message: types.Message, state: FSMContext, bot: Bot):
    text = (message.text or "").strip()
    if not text:
        await message.answer("Выбери город на клавиатуре или напиши свой.")
        return
    if text == "Другое":
        # Tap «Другое» → ask for the free-text value; the next message (any city) is stored.
        await message.answer("Напиши название своего города:", reply_markup=get_cancel_kb())
        return
    await state.update_data(city=text)
    await _advance("city", message, state, bot)


@router.message(Registration.source)
async def process_source(message: types.Message, state: FSMContext, bot: Bot):
    source = (message.text or "").strip()
    if not source:
        await message.answer("Выбери один из вариантов или напиши свой.")
        return
    if source == "Другое":
        await message.answer("Напиши свой вариант:", reply_markup=get_cancel_kb())
        return
    await state.update_data(source=source)
    await _advance("source", message, state, bot)


@router.message(Registration.local_committee)
async def process_local_committee(message: types.Message, state: FSMContext, bot: Bot):
    text = (message.text or "").strip()
    if not text:
        await message.answer("Выбери локальный комитет из списка или напиши свой.")
        return
    if text == "Другое":
        await message.answer("Напиши название своего ЛК:", reply_markup=get_cancel_kb())
        return
    await state.update_data(local_committee=text)
    await _advance("local_committee", message, state, bot)


@router.message(Registration.position)
async def process_position(message: types.Message, state: FSMContext, bot: Bot):
    text = (message.text or "").strip()
    if not text:
        await message.answer("Выбери позицию из списка или напиши свою.")
        return
    if text == "Другое":
        await message.answer("Напиши свою позицию:", reply_markup=get_cancel_kb())
        return
    await state.update_data(position=text)
    await _advance("position", message, state, bot)


@router.message(Registration.education_status)
async def process_education_status(message: types.Message, state: FSMContext, bot: Bot):
    status = (message.text or "").strip()
    if not status:
        await message.answer("Выбери один из вариантов.")
        return
    await state.update_data(education_status=status)
    if not status.startswith("Да"):
        await state.update_data(university="-", course="-", specialty="-", study_field="-")
    await _advance("education_status", message, state, bot)


@router.message(Registration.university)
async def process_university(message: types.Message, state: FSMContext, bot: Bot):
    uni = (message.text or "").strip()
    if not uni:
        await message.answer("Выбери ВУЗ из списка или напиши свой.")
        return
    if uni == "Другое":
        await message.answer("Напиши название своего ВУЗа:", reply_markup=get_cancel_kb())
        return
    await state.update_data(university=uni)
    await _advance("university", message, state, bot)


@router.message(Registration.course)
async def process_course(message: types.Message, state: FSMContext, bot: Bot):
    course = (message.text or "").strip()
    if not course:
        await message.answer("Выбери курс.")
        return
    await state.update_data(course=course)
    await _advance("course", message, state, bot)


@router.message(Registration.specialty)
async def process_specialty(message: types.Message, state: FSMContext, bot: Bot):
    text = (message.text or "").strip()
    if not text:
        await message.answer("Напиши или нажми «Пропустить».")
        return
    await state.update_data(specialty="-" if text == "Пропустить" else text)
    await _advance("specialty", message, state, bot)


@router.message(Registration.work_status)
async def process_work_status(message: types.Message, state: FSMContext, bot: Bot):
    answer = (message.text or "").strip()
    if answer not in ("Да", "Нет"):
        await message.answer("Выбери «Да» или «Нет».")
        return
    working = answer == "Да"
    await state.update_data(work_status=working)
    if not working:
        await state.update_data(work_sphere="-")
    await _advance("work_status", message, state, bot)


@router.message(Registration.work_sphere)
async def process_work_sphere(message: types.Message, state: FSMContext, bot: Bot):
    text = (message.text or "").strip()
    if not text:
        await message.answer("Напиши сферу работы или нажми «Пропустить».")
        return
    await state.update_data(work_sphere="-" if text == "Пропустить" else text)
    await _advance("work_sphere", message, state, bot)


@router.message(Registration.missing_skills)
async def process_missing_skills(message: types.Message, state: FSMContext, bot: Bot):
    text = (message.text or "").strip()
    if not text:
        await message.answer("Напиши или нажми «Пропустить».")
        return
    await state.update_data(missing_skills="-" if text == "Пропустить" else text)
    await _advance("missing_skills", message, state, bot)


@router.message(Registration.expectations)
async def process_expectations(message: types.Message, state: FSMContext, bot: Bot):
    text = (message.text or "").strip()
    if not text:
        await message.answer("Напиши или нажми «Пропустить».")
        return
    await state.update_data(expectations="-" if text == "Пропустить" else text)
    await _advance("expectations", message, state, bot)


@router.message(Registration.informal_day)
async def process_informal_day(message: types.Message, state: FSMContext, bot: Bot):
    text = (message.text or "").strip()
    if text not in ("Да", "Нет", "Буду только в онлайне"):
        await message.answer("Выбери один из вариантов.")
        return
    await state.update_data(informal_day=text)
    await _advance("informal_day", message, state, bot)


@router.message(Registration.attendance_format)
async def process_attendance_format(message: types.Message, state: FSMContext, bot: Bot):
    text = (message.text or "").strip()
    if text not in ("Offline", "Online"):
        await message.answer("Выбери «Offline» или «Online».")
        return
    await state.update_data(attendance_format=text)
    await _advance("attendance_format", message, state, bot)


@router.message(Registration.comments)
async def process_comments(message: types.Message, state: FSMContext, bot: Bot):
    text = (message.text or "").strip()
    if not text:
        await message.answer("Напиши или нажми «Пропустить».")
        return
    await state.update_data(comments="-" if text == "Пропустить" else text)
    await _advance("comments", message, state, bot)


# --- Conference (RusCo) reg-flow handlers ---

async def _store_choice(field: str, after: str, message: types.Message, state: FSMContext, bot: Bot):
    text = (message.text or "").strip()
    if not text:
        await message.answer("Выбери вариант на клавиатуре или напиши ответ.")
        return
    if text == "Другое":
        # Covers every «Другое»-bearing choice step routed through here (department,
        # aiesec_role, …): ask for the free-text value, stay in state, store the next reply.
        await message.answer("Напиши свой вариант:", reply_markup=get_cancel_kb())
        return
    await state.update_data(**{field: text})
    await _advance(after, message, state, bot)


async def _store_text(field: str, after: str, message: types.Message, state: FSMContext, bot: Bot):
    text = (message.text or "").strip()
    if not text:
        await message.answer("Напиши или нажми «Пропустить».")
        return
    await state.update_data(**{field: "-" if text == "Пропустить" else text})
    await _advance(after, message, state, bot)


@router.message(Registration.department)
async def process_department(message: types.Message, state: FSMContext, bot: Bot):
    await _store_choice("department", "department", message, state, bot)


@router.message(Registration.aiesec_role)
async def process_aiesec_role(message: types.Message, state: FSMContext, bot: Bot):
    await _store_choice("aiesec_role", "aiesec_role", message, state, bot)


@router.message(Registration.needs_certificate)
async def process_needs_certificate(message: types.Message, state: FSMContext, bot: Bot):
    await _store_choice("needs_certificate", "needs_certificate", message, state, bot)


@router.message(Registration.english_level)
async def process_english_level(message: types.Message, state: FSMContext, bot: Bot):
    await _store_choice("english_level", "english_level", message, state, bot)


@router.message(Registration.allergies)
async def process_allergies(message: types.Message, state: FSMContext, bot: Bot):
    await _store_text("allergies", "allergies", message, state, bot)


@router.message(Registration.food_pref)
async def process_food_pref(message: types.Message, state: FSMContext, bot: Bot):
    await _store_text("food_pref", "food_pref", message, state, bot)


@router.message(Registration.alumni_status)
async def process_alumni_status(message: types.Message, state: FSMContext, bot: Bot):
    await _store_choice("alumni_status", "alumni_status", message, state, bot)


@router.message(Registration.arrival)
async def process_arrival(message: types.Message, state: FSMContext, bot: Bot):
    await _store_choice("arrival", "arrival", message, state, bot)


@router.message(Registration.housing)
async def process_housing(message: types.Message, state: FSMContext, bot: Bot):
    await _store_choice("housing", "housing", message, state, bot)


@router.message(Registration.bed_sharing)
async def process_bed_sharing(message: types.Message, state: FSMContext, bot: Bot):
    # On «Да» the bed_partner step is enabled by _get_enabled_steps; on «Нет» it's skipped.
    await _store_choice("bed_sharing", "bed_sharing", message, state, bot)


@router.message(Registration.bed_partner)
async def process_bed_partner(message: types.Message, state: FSMContext, bot: Bot):
    await _store_text("bed_partner", "bed_partner", message, state, bot)


@router.message(Registration.transport)
async def process_transport(message: types.Message, state: FSMContext, bot: Bot):
    await _store_choice("transport", "transport", message, state, bot)


@router.message(Registration.cc_shop)
async def process_cc_shop(message: types.Message, state: FSMContext, bot: Bot):
    await _store_text("cc_shop", "cc_shop", message, state, bot)


@router.message(Registration.exp_organizers)
async def process_exp_organizers(message: types.Message, state: FSMContext, bot: Bot):
    await _store_text("exp_organizers", "exp_organizers", message, state, bot)


@router.message(Registration.exp_content)
async def process_exp_content(message: types.Message, state: FSMContext, bot: Bot):
    await _store_text("exp_content", "exp_content", message, state, bot)


@router.message(Registration.volunteer)
async def process_volunteer(message: types.Message, state: FSMContext, bot: Bot):
    await _store_choice("volunteer", "volunteer", message, state, bot)


# --- Finalize ---

async def _approve_text_for(participant_type: str | None, city_code: str | None = None) -> str:
    """D-15: per-track approval message. Resolution order:
    (1) party track and `approve_text__party` non-empty (truthy wins — an accidentally-empty
        override falls back rather than sending a blank message, same posture as _prompt's
        D-05 wording resolution) -> that text, WITHOUT any per-city layer. Phase 09.2-04
        (RESEARCH Open Question 2): composing a THIRD axis (track x city) on top of the
        existing party override was explicitly deferred — `approve_text__party` carries no
        `per_city` flag in SETTINGS_SCHEMA, a party delegate never gets a per-city approve
        text, only the global-vs-party split that already existed before this phase.
    (2) otherwise (non-party track, or an absent/empty party override) -> the BASE
        `approve_text` resolved through `cities.get_setting_for_city`, so a non-party
        delegate's city can override the global script.
    (3) otherwise -> DEFAULT_APPROVE_TEXT, reusing the same constant so there is only one
        copy of the default."""
    if _is_party_track(participant_type):
        override = await get_setting("approve_text__party")
        if override:
            return override
    return await get_setting_for_city("approve_text", city_code) or DEFAULT_APPROVE_TEXT


async def send_completion_and_bonus(bot: Bot, telegram_id: int, with_menu: bool = True,
                                     participant_type: str | None = None):
    """Deliver approve_text (post-approval script) + the configured registration bonus.
    Reused by the non-payment approval path, the free/single payment path (handlers.payment),
    and the admin receipt-confirm path (handlers.admin). Fail-soft: a blocked/unknown user
    never raises. `with_menu=False` skips the main-menu keyboard when the caller already sent it.
    Phase 5 (D-15): `participant_type` defaults to None so every pre-Phase-5 call site keeps
    compiling and behaving identically — only callers that know the track pass it explicitly.
    Text resolution is delegated entirely to _approve_text_for; no direct approve_text read
    remains in this function.
    Phase 09.2-04 (CITY-04): resolves the delegate's event_city ONCE, only when the cities
    module is on — this function receives only a chat id (no FSM data, unlike D-15's
    approve_user which already has a resolved user row for participant_type), so get_user is
    the only way to learn the city. Kept module-gated so an all-cities-off deployment adds
    zero extra DB reads to the approval path. A resolve failure falls back to city_code=None
    (global approve_text) — the approval message must still go out (T-05-04-04)."""
    city_code = None
    try:
        if await cities_module_on():
            user_row = await get_user(telegram_id)
            city_code = normalize_city(user_row.get("event_city") if user_row else None)
    except Exception as e:
        logger.error(f"per-city resolve for approve text failed for {telegram_id}: {e}")
        city_code = None
    try:
        complete_text = await _approve_text_for(participant_type, city_code)
        kwargs = {"parse_mode": "HTML"}
        if with_menu:
            kwargs["reply_markup"] = await get_main_menu_kb(telegram_id)
        await bot.send_message(telegram_id, complete_text, **kwargs)

        if await get_setting_typed("reg_bonus_enabled") == "on":  # REG-02: registry-backed
            bonus_caption = await get_setting("reg_bonus_caption") or "\U0001f381 Бонус за регистрацию!"
            bonus_photo = await get_setting("reg_bonus_photo_file_id")
            bonus_doc = await get_setting("reg_bonus_doc_file_id")
            if bonus_doc:
                await bot.send_document(telegram_id, bonus_doc, caption=bonus_caption, parse_mode="HTML")
            elif bonus_photo:
                await bot.send_photo(telegram_id, bonus_photo, caption=bonus_caption, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Failed to send completion/bonus to {telegram_id}: {e}")


async def approve_user(bot: Bot, telegram_id: int):
    """Send the post-approval welcome (complete text + main menu + bonus) to a user
    by chat id. Reused by the auto-approve path here and the manager manual-approve
    path (admin.py). Fail-soft: a blocked/unknown user never raises."""
    logger.info(f"user={telegram_id} action=approve_welcome")
    # Phase 5 (D-15): resolve the track ONCE, here, at the top — BEFORE the module-gate
    # branch below (which checks the payment setting and returns early). approve_user
    # receives only a chat id (no FSM data), so get_user() is the only way to learn the
    # track. Plan 05-05 Task 2 consumes this same resolved value to pass participant_type
    # into start_payment_step and must not add a second get_user call. Wrapped so a lookup
    # failure degrades to "full" rather than blocking the approval — an approved user must
    # always receive a message (T-05-04-04).
    try:
        user_row = await get_user(telegram_id)
        participant_type = (user_row or {}).get("participant_type") or "full"
    except Exception as e:
        logger.error(f"Failed to resolve participant_type for {telegram_id}, defaulting to 'full': {e}")
        participant_type = "full"
    try:
        # Phase 4 (D-09): payment module gates the welcome. When ON, the payment flow owns
        # all messaging (its own option/requisites/receipt path); the completion text + bonus
        # land after the manager confirms the receipt (admin rcpt_confirm) or immediately for
        # a free/single option. When OFF, behaviour is byte-identical to before.
        if await _is_module_enabled("payment_enabled"):
            from handlers.payment import start_payment_step  # local import avoids circular
            # Phase 5 (05-05): reuse the SAME participant_type resolved once above (D-15's
            # ordering guard) — no second get_user call.
            await start_payment_step(bot, telegram_id, participant_type)
            return

        await send_completion_and_bonus(bot, telegram_id, participant_type=participant_type)
    except Exception as e:
        logger.error(f"Failed to send approval welcome to {telegram_id}: {e}")


def _resume_person_name(data) -> str:
    """Build the human-readable resume filename stem: "<ФИО>_<username|telegram_id>".

    username in finalize is either "@name" or "-" (never None); a leading "@" is stripped
    and "-"/empty falls back to the telegram_id. Sanitization (unicode-safe) is done later
    by services.nextcloud._safe_name, so kept RAW here (cyrillic preserved).
    Example: full_name="Иван Петров", username="@qleafye" → "Иван Петров_qleafye".
    """
    name = (data.get("full_name") or "").strip()
    uname = (data.get("username") or "").strip().lstrip("@")
    if not uname or uname == "-":
        uname = str(data.get("telegram_id"))
    return f"{name}_{uname}"


def _single_flight(func):
    """Re-entrancy guard: не пускать второй параллельный вызов для того же telegram_id.

    Ночное ревью, находка #3. Состояние Registration.confirm живёт до ~20 секунд — внутри
    финализации идёт загрузка резюме в Nextcloud под asyncio.wait_for(timeout=20), а
    reply-клавиатура «Всё верно» всё это время у делегата на экране. Второй тап запускал
    ВТОРОЙ параллельный finalize: две строки в users и два append'а в Google-таблицу.

    Дубликат ничего не пишет делегату (первый вызов сам отправит сообщение о завершении,
    второе «подождите» было бы шумом) и молча выходит; освобождение — в finally, иначе
    любое исключение заперло бы делегата навсегда.

    Сделано декоратором, а не try/finally внутри функции, намеренно: тело
    finalize_registration остаётся нетронутым — ни строки переиндентации, диff читаемый,
    публичные имя и сигнатура сохранены (functools.wraps), поэтому оба существующих вызова
    и любой будущий третий проходят через гвард автоматически.
    """

    @functools.wraps(func)
    async def wrapper(message: types.Message, state: FSMContext, bot: Bot):
        uid = message.from_user.id
        # claim: между проверкой и добавлением нет await → атомарно в однопоточном asyncio
        if uid in _FINALIZING_USERS:
            logger.warning(f"Duplicate finalize ignored for {uid} (already in flight)")
            return None
        _FINALIZING_USERS.add(uid)
        try:
            return await func(message, state, bot)
        finally:
            _FINALIZING_USERS.discard(uid)

    return wrapper


@_single_flight
async def finalize_registration(message: types.Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    data["telegram_id"] = message.from_user.id
    data["username"] = f"@{message.from_user.username}" if message.from_user.username else "-"
    data["registration_date"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    data.setdefault("email", "-")
    data.setdefault("phone", "-")
    data.setdefault("city", "-")
    data.setdefault("is_aiesec_member", False)
    data.setdefault("source", "Реферальная ссылка" if data.get("referrer_id") else "Самостоятельно")
    data.setdefault("source_details", f"Referrer ID: {data.get('referrer_id', '-')}")
    data.setdefault("education_status", "-")
    data.setdefault("university", "-")
    data.setdefault("course", "-")
    data.setdefault("specialty", "-")
    data.setdefault("work_status", False)
    data.setdefault("work_sphere", "-")
    data.setdefault("missing_skills", "-")
    data.setdefault("expectations", "-")
    data.setdefault("local_committee", "-")
    data.setdefault("position", "-")
    # IN-01: expectations_ar is vestigial (no step ever populates it). The dead setdefault is
    # dropped; the "Ожидания (AR)" sheet column is left in place to avoid a mid-season sheet
    # width change (removing it would require an admin «Пересобрать таблицу» to re-align).
    data.setdefault("informal_day", "-")
    data.setdefault("attendance_format", "-")
    data.setdefault("comments", "-")
    data.setdefault("resume_file_id", None)
    data.setdefault("resume_text", None)
    data.setdefault("resume_url", None)

    # Phase 07.3 (04, RET-01/RET-02): resolve season/prev_season BEFORE add_user, in its own
    # fail-soft try/except -- a season-resolve error must never lose an already-collected
    # application (T-073-04-07). `prior` (the returning delegate's snapshot from plan 03,
    # empty for a first-time delegate) is read once here and reused after add_user below for
    # the payment-reset decision.
    prior = data.get("_prior_answers") or {}
    season = None
    try:
        season = (await get_setting("event_season") or "").strip() or None
        data["season"] = season
        if prior:
            # CONTEXT B: a genuinely returning delegate always gets a prev_season, even when
            # their prior row predates the season concept ("legacy" -- same literal
            # mark_season_ended uses, per plan 01's D-03; never shown raw to a manager, plan 05
            # translates it). A first-time delegate (prior empty) leaves prev_season unset --
            # data.get('prev_season') rides through add_user as None like any other never-set
            # column.
            data["prev_season"] = (prior.get("season") or "").strip() or "legacy"
    except Exception as e:
        logger.error(f"Season resolve failed for {message.from_user.id}: {e}")
        season = None
        data["season"] = None
    # T-073-04-?? / RESEARCH: referrer_id is deliberately NEVER read from `prior` -- it is not
    # in STEP_TO_COLUMN and `data['referrer_id']` only ever comes from THIS session's deep-link
    # (cmd_start/_start_registration_flow). A returning delegate's past referral is not carried
    # forward; no code change is needed here, this comment plus a dedicated test pin the
    # invariant so a future edit can't silently start reading `prior.get('referrer_id')`.

    # WK6/16c: resume (file OR text) → Nextcloud WebDAV PUT + deep-link into the manual folder
    # share, captured BEFORE add_user so the URL is persisted in DB and lands in the sheet row.
    # File is named "<ФИО>_<username|id>.<ext>"; text resume is uploaded as a "<...>.txt" file.
    # Fully fail-soft: bounded by a 20s wait_for, upload_* never raise, and any timeout/error
    # leaves resume_url None so registration always completes even if Nextcloud is down.
    try:
        if data.get("resume_file_id"):
            ext = os.path.splitext(data.get("resume_file_name") or "")[1]
            fname = f"{_resume_person_name(data)}{ext}"
            url = await asyncio.wait_for(
                upload_resume(bot, data["resume_file_id"], fname), timeout=20
            )
            if url:
                data["resume_url"] = url
        elif data.get("resume_text"):
            fname = f"{_resume_person_name(data)}.txt"
            url = await asyncio.wait_for(
                upload_text_resume(data["resume_text"], fname), timeout=20
            )
            if url:
                data["resume_url"] = url
    except Exception as e:  # IN-02: TimeoutError is already a subclass of Exception
        logger.error(f"Nextcloud resume upload failed for {message.from_user.id}: {e}")

    # LOW (consent compliance): consent acceptance is enforced only by FSM step ordering during
    # the flow — never re-verified at the end. Re-check here as a compliance audit: if consent is
    # enabled and any required consent has no recorded D-02 acceptance row for this user, log a
    # warning. We do NOT block — an already-consenting user must never be locked out by a
    # transient recording hiccup; the recorded rows remain the source of truth, this only
    # surfaces a gap that should never occur.
    try:
        if await _is_module_enabled("consent_enabled"):
            _required = {k for _lbl, k in await _consent_entries()}
            if _required:
                _recorded = set(await get_user_consents(message.from_user.id))
                _missing = _required - _recorded
                if _missing:
                    logger.warning(
                        f"Consent compliance gap for {message.from_user.id}: missing "
                        f"{sorted(_missing)} (recorded={sorted(_recorded)}) — proceeding."
                    )
    except Exception as e:
        logger.error(f"Consent re-verify failed for {message.from_user.id}: {e}")

    # Phase 5 (D-01): a flow that never saw a party link writes the default explicitly rather
    # than relying on the column default.
    data.setdefault("participant_type", "full")
    # Ночное ревью, находка #4: единственный неогороженный await во всей финализации.
    # Падение SQLite («database is locked») уходило в глобальный @dp.errors() (main.py),
    # который его молча глотал: заявка не сохранена, делегат уверен, что зарегистрировался.
    # Теперь — ранний выход БЕЗ очистки состояния: FSM остаётся в Registration.confirm
    # (повторный тап «Всё верно» сработает), reg_started не стирается (регистрация
    # действительно не завершена, нуджи должны продолжать работать), в таблицу ничего
    # не едет. Клавиатуру прикладываем заново: one-time клава уже сложилась.
    try:
        await add_user(data)
    except Exception as e:
        logger.error(f"add_user failed for {message.from_user.id}: {e}")
        await _safe_answer(
            message,
            "Не получилось сохранить заявку — техническая ошибка на нашей стороне. "
            "Ответы не потерялись: подождите минуту и нажмите «Всё верно» ещё раз. "
            "Если не выйдет и со второго раза — напишите организаторам.",
            reply_markup=get_confirm_kb(),
        )
        return

    # Phase 07.3 (04, RET-02/WR-06): payment reset lives in its own try/except AFTER add_user
    # succeeds -- add_user itself never touches payment columns (WR-06), so "anketa first,
    # reset second" gives a correct result no matter which of the two operations would fail.
    # Condition is deliberately narrower than "any returning delegate": only fires when the
    # delegate is ACTUALLY entering a new season (CONTEXT B: «новый сезон = новая оплата»). A
    # rejected delegate re-applying within the SAME season keeps their existing payment state
    # (the WR-06 invariant payment columns are excluded from add_user's ON CONFLICT for).
    if prior and season and (prior.get("season") or "").strip() != season:
        try:
            await reset_payment_for_new_season(message.from_user.id)
        except Exception as e:
            logger.error(f"reset_payment_for_new_season failed for {message.from_user.id}: {e}")

    logger.info(
        f"user={message.from_user.id} action=registration_complete "
        f"mode={await get_setting('registration_mode') or 'short'} name={data.get('full_name')!r}"
    )

    # SCHED-02: registration finished — drop the dropout row (fail-soft).
    try:
        await clear_reg_started(message.from_user.id)
    except Exception as e:
        logger.error(f"Failed to clear reg_started for {message.from_user.id}: {e}")

    # Phase 2 (D-01..D-04): decide approval status from form type + per-form setting, persist it.
    # Считаем статус ДО записи в таблицу, чтобы колонка «Статус» (Таня п.5) заполнилась
    # сразу правильным значением (Новая/Одобрена), а не дефолтом.
    # REG-02 (06-06): registry-backed — full_approval/short_approval/registration_mode carry
    # the SAME defaults ("manual"/"auto"/"short") the old `or "<default>"` idiom applied.
    reg_mode = await get_setting_typed("registration_mode")
    full_setting = await get_setting_typed("full_approval")  # BLOCKER-1
    short_setting = await get_setting_typed("short_approval")
    # Phase 5 (D-13): party tracks resolve status from their own independent setting.
    # RAW-read site migrated (06-06): party_approval's registry default ("manual") is
    # identical to _decide_status's own `party_setting or "manual"` fallback below.
    party_setting = await get_setting_typed("party_approval")
    status = _decide_status(
        reg_mode, full_setting, short_setting,
        participant_type=data.get("participant_type", "full"), party_setting=party_setting,
    )
    data["status"] = status
    try:
        await set_user_status(message.from_user.id, status)
    except Exception as e:
        logger.error(f"Failed to set status for {message.from_user.id}: {e}")

    # HG-01: persist the subscription flag NOW. cmd_start ran its check BEFORE this user's row
    # existed (first /start on a brand-new user = UPDATE 0 rows, silently dropped), so a
    # first-touch registrant — the majority — would otherwise stay subscribed=NULL forever and
    # never land in the «не подписаны» broadcast segment. add_user above guarantees the row here.
    # Fail-soft + fail-open (D-07): a check error never blocks registration completion.
    try:
        _sub_channel = _normalize_channel_ref(await get_setting("contact_tg"))
        if _sub_channel is not None:
            _sub_result = await is_subscribed(bot, _sub_channel, message.from_user.id)
            if _sub_result is not None:
                await set_user_subscribed(message.from_user.id, _sub_result)
    except Exception as e:
        logger.warning(f"Subscription persist skipped for {message.from_user.id}: {e}")

    # Fire-and-forget the Google Sheet write so the user is NOT blocked on a ~5s network
    # round-trip (auth + open + append, plus up to 3 retries with sleeps). append_to_sheet /
    # append_to_party_sheet are fail-soft and log their own errors. Build the row inline (needs
    # current settings), then hand the network I/O to the background.
    #
    # Phase 5 (D-11/D-12) / Phase 7 (SHORT-02): EXCLUSIVE routing — a registration goes to
    # exactly one tab (party / short / main), never more than one. Resolved via _sheet_dispatch
    # so the exclusivity property is provable by a test that doesn't run this whole function.
    # Phase 07.1 (CITY-02): city selects the TAB, track (via _sheet_dispatch) still selects the
    # COLUMNS — _row_fn/the row itself are unchanged; only the append destination can move to a
    # non-default city's named tab. Exactly ONE append per registration either way.
    try:
        _row_fn, _append_fn = _sheet_dispatch(data.get("participant_type"))
        _row = await _row_fn(data)
        _tab = await city_row_tab(data.get("event_city"), data.get("participant_type"))
        if _tab is None:
            _spawn(_append_fn(_row))
        else:
            _spawn(append_to_named_sheet(_tab, _row))
    except Exception as e:
        logger.error(f"Failed to schedule sheet append for {message.from_user.id}: {e}")

    # Admin notify: always for approved; for pending only when pending_notify_mode='instant' (D-15).
    notify_admins = status == "approved" or (
        status == "pending" and await get_setting_typed("pending_notify_mode") == "instant"  # REG-02
    )
    if config.ADMIN_IDS and notify_admins:
        safe_name = html.escape(str(data.get("full_name", "-")))
        safe_username = html.escape(str(data.get("username", "-")))
        admin_text = (
            f"\U0001f195 <b>Новая регистрация!</b>\n"
            f"\U0001f464 {safe_name} ({safe_username})"
        )
        if status == "pending":
            admin_text += "\n⏳ Ожидает одобрения (/admin → Заявки)"
        if data.get("local_committee") and data["local_committee"] != "-":
            admin_text += f"\n\U0001f3e2 {html.escape(str(data['local_committee']))}"
        if data.get("position") and data["position"] != "-":
            admin_text += f"\n\U0001f454 {html.escape(str(data['position']))}"
        if data.get("age"):
            admin_text += f"\n\U0001f382 {data['age']}"
        safe_source = html.escape(str(data.get("source", "-")))
        if safe_source != "-":
            admin_text += f"\n\U0001f4dd {safe_source}"

        # Phase 09.2 (D): заявка из города уходит менеджерам этого города; None = город не
        # выбран → сегодняшний глобальный фан-аут; фолбэк «после фильтра пусто → все» живёт
        # внутри capability_holders (не дублируется здесь). Grep-проверка (09.2-02 Task 3):
        # data.get("event_city") уже читается строкой выше (city_row_tab, line ~3008) в ЭТОЙ
        # ЖЕ функции — доступен на всех треках (short/full/party идут через один и тот же
        # finalize_registration), отдельного get_user не требуется.
        await notify_by_capability(bot, "moderate_reg", admin_text, parse_mode="HTML", city=data.get("event_city"))  # D-13

    await state.clear()
    # Tatiana: «поздравляем»-скрипт приходит сразу после регистрации — всем (и pending, и
    # approved). Approve/reject досылают свои отдельные скрипты позже.
    # Phase 09.2-04 (CITY-04/CONTEXT B): resolved by the delegate's own event_city;
    # data.get("event_city") is already read a few lines above (city_row_tab) in this SAME
    # function on every track (short/full/party), no extra get_user call needed.
    try:
        submitted = await get_setting_for_city("reg_complete_text", data.get("event_city")) or DEFAULT_REG_COMPLETE_TEXT
    except Exception as e:
        logger.error(f"get_setting_for_city(reg_complete_text) failed for {message.from_user.id}: {e}")
        submitted = await get_setting("reg_complete_text") or DEFAULT_REG_COMPLETE_TEXT
    try:
        await message.answer(submitted, reply_markup=ReplyKeyboardRemove(), parse_mode="HTML")
    except Exception:
        await message.answer(submitted, reply_markup=ReplyKeyboardRemove())
    if status == "approved":
        await approve_user(bot, message.from_user.id)
