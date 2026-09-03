"""Phase 13 REFAC (13-02, REFAC-02): registration data registries + sheet-schema plumbing
shared by handlers/registration.py and handlers/admin.py.

Extracted from handlers/registration.py to remove admin.py's reach into a handler module
(CONCERNS.md "Fix approach"): admin.py used to import 17 names directly out of the
registration router module, including two underscore-private symbols. This module holds NO
Router and NO @router.* handler -- it is pure data + pure/async helper functions, importable
by both handlers/registration.py (which re-imports everything it still needs at its own
top, since these definitions used to live there) and handlers/admin.py.

Behavior is byte-for-byte unchanged from the pre-move code -- every definition below is a
verbatim relocation, not a rewrite. Where a moved function needed a function-body-local
import to dodge an import cycle back into handlers.registration (approve_user's historical
`from handlers.payment import start_payment_step`, and the new local imports this move
required for incomplete_city_batches), that pattern is used exactly as registration.py
already used it elsewhere.
"""
import json
import logging

from aiogram import Bot

from database.db import get_setting, set_setting, get_user
from settings_schema import get_setting_typed
# Phase 19 (Mini App): подписи анкеты живут в корневом aiogram-free `reg_labels.py`;
# здесь — реэкспорт ТЕХ ЖЕ объектов (admin.py, admin_reg_config.py, admin_moderation.py
# импортируют их отсюда как раньше).
from reg_labels import REG_LABELS, STATUS_LABELS  # noqa: F401
# Phase 21 (21-01, FORM-SYNC-01): REG_FLOW и его непосредственные зависимости переехали в
# корневой aiogram-free reg_engine.py (та же причина, что у REG_LABELS выше — веб-процесс
# Mini App не должен импортировать handlers.* и тянуть за собой весь бот). Реэкспорт ТЕХ ЖЕ
# объектов — handlers/registration.py, handlers/admin.py, admin_reg_config.py и тесты
# продолжают импортировать их отсюда как раньше.
from reg_engine import (  # noqa: F401
    REG_FLOW, _is_party_track, SHORT_TRACK, _is_short_track,
    REG_DEFAULTS, _is_step_enabled, _is_module_enabled,
    STEP_TO_COLUMN, REG_STEP_TYPES,
)
from cities import cities_module_on, normalize_city, is_default_city, city_tab_base, tab_suffix, get_setting_for_city
from keyboards.builders import get_main_menu_kb

logger = logging.getLogger(__name__)

# --- Registration Flow Engine ---
# REG_FLOW moved to reg_engine.py (Phase 21, 21-01, FORM-SYNC-01); imported above (re-export
# block, same reason/pattern as REG_LABELS).

# step_key → its reg_q_* setting key, for resolving human labels in dropout analytics.
_STEP_TO_SETTING = {step_key: setting_key for step_key, setting_key, _t in REG_FLOW}


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


# REG_DEFAULTS moved to reg_engine.py (Phase 21, 21-01) alongside REG_FLOW; imported above.
# The NAME is retained unchanged because handlers/admin.py still imports/iterates it
# (admin.py:59 import, :501, :2097 _is_question_on, :2248, and the preset bulk-write loop at
# :2336/:2412) — deleting the name would break those call sites for no benefit.

# REG_LABELS — см. корневой reg_labels.py (Phase 19), импорт вверху модуля.

# --- Event-type presets (admin one-tap bulk toggle) ---
# A preset lists the reg_q_* keys to turn ON (everything else in REG_DEFAULTS is turned
# OFF) plus the payment module flag. Applying a preset is an explicit admin action that
# writes the same settings the per-question toggles write — it changes NOTHING until
# tapped, so live bots keep their current flow. Extra questions can still be flipped on
# individually afterwards (see REG_CATEGORIES «➕ Экстра»).
REG_PRESETS = {
    "forum": {
        "label": "🏛 Форум (Юлид)",
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


# _is_party_track/SHORT_TRACK/_is_short_track moved to reg_engine.py (Phase 21, 21-01);
# imported above (re-export block).


def _sheet_details(data: dict) -> str:
    parts = []
    if data.get("referrer_id"):
        parts.append(f"Referrer ID: {data['referrer_id']}")
    # Phase 21 (21-08, D-16): пометка правки — «✏️ Изменена дд.мм (поля: ...)». Дописывается
    # ВТОРЫМ элементом (после Referrer ID, если он есть), а не отдельной колонкой листа
    # (Pitfall 4 — новая колонка посреди сезона сдвигает уже записанные строки). Значение
    # проставляет services.reg_finalize.post_finalize перед сборкой строки — здесь только
    # читаем готовую строку, никакой логики форматирования.
    if data.get("_edited_note"):
        parts.append(str(data["_edited_note"]))
    return " | ".join(parts) if parts else "-"


# status код БД → человеческий ярлык в колонке «Статус» (Таня, п.5). «Новая» = ещё не
# смотрели (pending), «Одобрена»/«Отклонена» — после решения менеджера. Совпадает со
# списком значений выпадашки в services.sheets.STATUS_LABELS.
# Сам словарь — в корневом reg_labels.py (Phase 19), импорт вверху модуля.


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
    ("Роль АЙСЕК", "reg_q_aiesec_role", lambda d: d.get("aiesec_role") or "-"),
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


# _is_step_enabled moved to reg_engine.py (Phase 21, 21-01); imported above.


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


# --- Phase 07.1 (CITY-02, plan 07.1-02): city selects the TAB, track selects the COLUMNS ----
# _sheet_dispatch (handlers/registration.py) stays the sole source of ROW BUILDER +
# exclusivity; the helper below only resolves a TAB NAME. It must never be merged into
# _sheet_dispatch's signature.
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
    non-default city tab is never created/wiped by this path.

    13-02 (REFAC-02): the три helper below (incomplete_sheet_headers/city_incomplete_tab/
    incomplete_sheet_row) stay in handlers/registration.py — they are wired into
    _is_step_enabled_for_track, the live per-track FSM gate, which is registration-flow
    machinery rather than admin-shared sheet plumbing. Local import here mirrors the
    function-body-local-import pattern registration.py's own approve_user already used
    to dodge a cycle."""
    from handlers.registration import get_incomplete_rows_with_city, incomplete_sheet_headers, city_incomplete_tab, incomplete_sheet_row
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


DEFAULT_APPROVE_TEXT = "Твоя заявка одобрена! Добро пожаловать 🎉"


# _is_module_enabled moved to reg_engine.py (Phase 21, 21-01); imported above.


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
