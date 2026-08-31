"""Phase 13 (13-06, REFAC-01): settings seam.

`admin.py` (contiguous slice, originally lines 685-2358) moved byte-for-byte onto the SAME
shared `admin.router` (13-02..13-05 shared-router seam-import technique) — the settings landing/
group screens, every toggle_*/settings_toggle_* feature switch, the EditSetting wizard (photo/
file/text/tab-confirm), consent-PDF handlers, sheets sync/rebuild, and CSV exports. Not split
further into a second `admin_sheets.py`: the sync/rebuild and export/sheets-tab handlers are not
mutually contiguous in the original file (sync/rebuild sits before the EditSetting message
handlers, export/sheets-tab sits after) — a two-file split would require two separate
seam-import insertion points to keep the 13-01 snapshot's registration order, which is not a
clean single-position seam. Kept as one file; see 13-06-SUMMARY.md's Known Gap for the
line-count consequence (same class as 13-04's admin_gamification.py / 13-05's admin_cities.py).
"""
import csv
import io
import html as html_module
import logging

from aiogram import F, types
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from config import config
from settings_schema import SETTINGS_SCHEMA, get_setting_typed
from database.db import (
    get_all_users_dicts,
    export_users_csv,
    get_setting,
    set_setting,
    delete_setting,
    get_dropout_step_stats,
    get_staff_city,
)
from services.sheets import (
    get_existing_sheet_ids,
    append_rows_to_sheet,
    ensure_sheet_header,
    sync_named_worksheet,
    rebuild_main_sheet,
    REFUSED_UNPINNED_TAB,
    _reset_sheet_cache,
    tab_row_count,
    ensure_named_sheet_header,
    get_existing_named_sheet_ids,
    append_rows_to_named_sheet,
)
from handlers.states import EditSetting
from handlers.settings_validation import validate_setting_value, is_command_like
from services.game_digest import game_submit_notify_button_text  # Quick 260822: тумблер дайджеста сдач
from keyboards.builders import MENU_BUTTONS
from handlers.reg_schema import (
    REG_FLOW,
    active_sheet_headers,
    set_sheet_schema,
    _sheet_value_map,
    dropout_step_label,
    city_row_tab,
    incomplete_city_batches,
)
from cities import (
    city_label,
    cities_module_on,
    admin_selected_city,
    city_codes,
    normalize_city,
    ALL_CITIES,
    is_per_city,
    per_city_key,
    split_per_city_key,
    city_override_codes,
    get_setting_typed_for_city,
    PER_CITY_SEP,
)
from handlers.admin_core import admin_keyboard_for, _admin_city_view
from handlers.admin import router

logger = logging.getLogger(__name__)

# INVARIANT (13-01 cap-test, extended by 13-04 to scan every handlers/admin*.py file): every
# `@router.*` decorator below MUST fit on ONE line.


# --- Settings ---

# REG-03: the event-group text/enum entries are GENERATED from settings_schema.SETTINGS_SCHEMA
# (single source of truth, D-13) instead of hand-written literals. Order is pinned explicitly
# (not a dict-order assumption) so the settings screen stays byte-identical to the
# pre-registry literal table. Remaining (unmigrated) groups below stay literal tuples — no
# change — until their own migration wave (coexistence invariant, SC#3).
_EVENT_FIELD_ORDER = [
    "event_date", "event_time", "event_place_name", "event_place_address",
    "contact_person", "contact_vk", "contact_tg", "start_text", "start_text_registered",
    "start_text_returning",
    # Phase 17.1 (17.1-02): recall/возвращение — CTA под баннером прошлого сезона и два
    # экрана «прошлый ответ» анкеты, рядом со start_text_returning (то же «возвращение»).
    "start_returning_cta_text", "recall_resume_prompt_text", "recall_generic_prompt_text",
    # Phase 17.1 (17.1-03): empty-state'ы «📅 Программа»/«🗣 Спикеры»/«📞 Контакты» и оба
    # экрана «❓ Задать вопрос» — тексты информационных кнопок меню, рядом с медиа/контактами.
    "program_empty_text", "speakers_empty_text", "contacts_empty_text",
    "ask_question_prompt_text", "ask_question_sent_text",
    # Опросы: вступление перед опросом — делегатский текст, рядом с другими текстами меню.
    "poll_intro_text",
    "event_name", "event_season", "event_type",
]
_EVENT_FIELDS = [
    (k, SETTINGS_SCHEMA[k]["label"], SETTINGS_SCHEMA[k]["prompt"])
    for k in _EVENT_FIELD_ORDER
]

# REG-01/REG-03 (06-02): reg/pay/party/consent groups GENERATED from settings_schema.
# SETTINGS_SCHEMA (same computed-view splice as the event pilot, D-13) — every text/list/
# int/date key that used to be a hand-written SETTINGS_FIELDS tuple now lives ONLY in the
# registry; order is pinned per group (not registry dict-insertion order) so the on-screen
# order stays byte-identical to the pre-migration literal tables.
_REG_FIELD_ORDER = [
    "source_options", "city_options", "study_field_options",
    "goal_options", "formats_options", "university_options",
    # Phase 17.1 (17.1-03, schema-completeness): экран выбора города при /start — читался из
    # bot_settings, но менеджер его в UI не видел.
    "city_fork_text",
]

# Phase 20 (20-01, ADMIN-IA-01): группа «📋 Заявки» — всё, что делегат видит ПОСЛЕ подачи
# анкеты (подтверждение, одобрение, отклонение, «на рассмотрении», предотбор, догонялка) плюс
# тайминг сводки для менеджера. До фазы 20 эти тексты лежали в «📝 Регистрация» вперемешку со
# списками вариантов вопросов, и «После одобрения» приходилось искать в настройках анкеты.
# SETTINGS_SCHEMA не тронут: физическое место ключа в реестре прежнее, переехала только
# группировка экрана.
_APPS_FIELD_ORDER = [
    "reg_complete_text", "approve_text", "reject_text",
    # Phase 17.1 (17.1-01): гейт «заявка на рассмотрении» — рядом с reject_text, обе ветки
    # одного `_gate_decision` редактируются в одном месте.
    "pending_gate_text",
    "pending_reminder_interval",
    # Phase 17.1 (17.1-03, schema-completeness): тексты гейта предотбора — читались из
    # bot_settings, но менеджер их в UI не видел.
    "preselect_no_username_text", "preselect_fail_text", "preselect_link",
    # Quick 260819 (schema-completeness): догонялка брошенных анкет (порог и текст).
    "nudge_after_minutes", "nudge_text",
]
_PAY_FIELD_ORDER = [
    "payment_options", "payment_requisites", "payment_requisites_by_lc",
    "payment_deadline", "payment_reminder_text", "payment_overdue_text", "penalty_schedule",
    # Phase 17.1 (17.1-02): делегатские экраны платёжного потока (handlers/payment.py) —
    # в порядке, в котором делегат их видит: выбор варианта → экран оплаты → «оплачу позже»
    # → «чек получен».
    "payment_option_picker_header_text", "payment_details_template_text",
    "payment_pay_later_text", "payment_pay_later_menu_hint_text",
    "payment_receipt_received_text",
]
_PARTY_FIELD_ORDER = [
    "party_closed_text", "approve_text__party",
    # Phase 17.1 (17.1-03, schema-completeness): вопрос развилки формата при /start.
    "party_fork_text",
]
_CONSENT_FIELD_ORDER = [
    "consent_button_text", "consent_list", "consent_version", "consent_recollect_text",
]

# Phase 09.1 (A): every editable text in the free-form submission flow, one group
# «🎮 Геймификация» — promo prompts by type, the general/multi-type fallback, the
# "жми Готово" hint, the button's own label, the empty-submission hint, and the accepted text.
_GAME_FIELD_ORDER = [
    "game_proof_prompt_photo", "game_proof_prompt_pdf", "game_proof_prompt_text",
    "game_proof_prompt_link", "game_proof_prompt_any", "game_proof_done_hint",
    "game_proof_done_button", "game_proof_empty_hint", "game_submit_accepted_text",
    # Phase 16 (16-02, GAME-UI-02): счётчик сдачи (Экран 3) -- рядом с остальными текстами сдачи.
    "game_proof_collected_template", "game_proof_remove_last_button",
    "game_resubmit_limit", "game_submit_digest_minutes", "coins_manual_notify_text",
    # Phase 16 (16-04, GAME-UI-03): quick-pick сумм ручных монет -- рядом с текстом уведомления.
    "coins_manual_amount_presets",
    # Quick 260819-gtl (CONTEXT.md decision 8): title/photo wizard step prompts.
    "game_task_title_prompt", "game_task_photo_prompt",
    # Phase 16 (16-03, GAME-UI-03): превью «как видит делегат» + финальный шаг визарда.
    "game_task_preview_intro", "game_wizard_preview_title", "game_wizard_publish_btn",
    # Phase 16 (16-01, GAME-UI-01): RU-категории + тексты редизайна «🎯 Задания»/«🪙 Баланс».
    "game_category_label_light", "game_category_label_medium", "game_category_label_hard",
    "game_category_label_referral", "game_category_label_special",
    "game_task_list_empty", "game_task_list_page_label", "game_task_detail_status_label",
    "balance_screen_header", "balance_history_empty",
    "balance_source_manual_label", "balance_source_task_label",
    # Phase 17.1 (17.1-01): RU-подписи типов подтверждения + подсказка «срок вышел» рядом с
    # RU-категориями выше; «🏆 Рейтинг», «📜 История монет» и рефералка — хвост делегатских
    # текстов монетного блока, доехавший до реестра.
    "game_proof_type_label_photo", "game_proof_type_label_pdf",
    "game_proof_type_label_text", "game_proof_type_label_link",
    "game_proof_type_unspecified_text", "game_task_overdue_hint_text",
    "leaderboard_header_text", "leaderboard_rank_line_text", "leaderboard_empty_text",
    "balance_history_header_text",
    "referral_link_prompt_text", "referral_list_header_text", "referral_list_empty_text",
]

# Phase 14 (CFG-01): group «🔧 Система» — proxy timings that used to live only in .env.
_SYSTEM_FIELD_ORDER = [
    "proxy_recheck_seconds", "proxy_connect_timeout",
    # Quick 260819 (schema-completeness): интервалы фоновых джоб (после перезапуска).
    "nudge_scan_minutes", "allowlist_refresh_minutes", "incomplete_sync_hours",
]

# Quick 260815-3hw (TABS-01/02/03): every Google Sheets tab NAME in one group — «📄 Вкладки
# таблицы». short_sheet_tab/party_sheet_tab moved here from reg/party (physically relocated in
# settings_schema.py, not duplicated). Order is the on-screen order, not registry insertion order.
_SHEETS_FIELD_ORDER = [
    "main_sheet_tab", "short_sheet_tab", "party_sheet_tab", "incomplete_sheet_tab",
    "game_matrix_tab", "game_history_tab", "preselect_tab",
    "city_tab_suffix__short", "city_tab_suffix__party", "city_tab_suffix__incomplete",
    "city_tab_suffix__game", "city_tab_suffix__game_history",
    "polls_sheet_tab",  # опросы: вкладка выгрузки результатов
]

_REG_FIELDS = [(k, SETTINGS_SCHEMA[k]["label"], SETTINGS_SCHEMA[k]["prompt"]) for k in _REG_FIELD_ORDER]
_APPS_FIELDS = [(k, SETTINGS_SCHEMA[k]["label"], SETTINGS_SCHEMA[k]["prompt"]) for k in _APPS_FIELD_ORDER]
_PAY_FIELDS = [(k, SETTINGS_SCHEMA[k]["label"], SETTINGS_SCHEMA[k]["prompt"]) for k in _PAY_FIELD_ORDER]
_PARTY_FIELDS = [(k, SETTINGS_SCHEMA[k]["label"], SETTINGS_SCHEMA[k]["prompt"]) for k in _PARTY_FIELD_ORDER]
_CONSENT_FIELDS = [(k, SETTINGS_SCHEMA[k]["label"], SETTINGS_SCHEMA[k]["prompt"]) for k in _CONSENT_FIELD_ORDER]
_SHEETS_FIELDS = [(k, SETTINGS_SCHEMA[k]["label"], SETTINGS_SCHEMA[k]["prompt"]) for k in _SHEETS_FIELD_ORDER]
_GAME_FIELDS = [(k, SETTINGS_SCHEMA[k]["label"], SETTINGS_SCHEMA[k]["prompt"]) for k in _GAME_FIELD_ORDER]
_SYSTEM_FIELDS = [(k, SETTINGS_SCHEMA[k]["label"], SETTINGS_SCHEMA[k]["prompt"]) for k in _SYSTEM_FIELD_ORDER]

# NOTE: reg_university_mode и edu_conditional вынесены в кнопки-переключатели (build_settings_keyboard).
# PDF согласий грузятся в разделе «🧾 PDF согласий».
# Phase 5 (D-11a/D-13): party-track text settings (party_enabled/party_fork_question/
# party_approval are toggle buttons in build_settings_keyboard, not here).
SETTINGS_FIELDS = (
    _EVENT_FIELDS + _REG_FIELDS + _APPS_FIELDS + _PAY_FIELDS + _PARTY_FIELDS + _CONSENT_FIELDS
    + _SHEETS_FIELDS + _GAME_FIELDS + _SYSTEM_FIELDS
)

# Phase 5 (D-11a): default text shown in render_settings_text when a text setting is unset,
# so the manager sees what users actually receive today, not a bare "не указано". REG-01
# (06-02): derived from the registry `default` field instead of a hand-written literal dict
# (T-06-06) — restricted to `type == "text"` entries with a genuinely non-empty default so a
# functional parse-fallback default (e.g. pending_reminder_interval's int default 1800) is
# never mistaken for a display default.
_SETTINGS_DISPLAY_DEFAULTS = {
    k: v["default"] for k, v in SETTINGS_SCHEMA.items()
    if v["type"] == "text" and v.get("default") not in (None, "")
}

# Quick 260724-c0x: group→keys grouping (NOT a per-key metadata registry) so the settings
# landing screen can route into per-group sub-screens instead of dumping every field's value
# inline. Shape mirrors REG_CATEGORIES (handlers/registration.py) — (label, token, [keys]).
# REG-03: the "event" row's key list is generated from SETTINGS_SCHEMA (registry is the
# source, D-13) — same pinned _EVENT_FIELD_ORDER used to build _EVENT_FIELDS above, filtered
# to the text/enum keys (photo/file keys are handled separately by the event branch in
# render_settings_group_text/build_settings_group_keyboard via PHOTO_FIELDS/FILE_FIELDS,
# unchanged per D-10).
_EVENT_GROUP_KEYS = [
    k for k in _EVENT_FIELD_ORDER
    if SETTINGS_SCHEMA[k]["type"] in ("text", "enum")
]

SETTINGS_GROUPS = [
    ("🎪 Событие/Медиа", "event", _EVENT_GROUP_KEYS),
    ("📝 Регистрация", "reg", _REG_FIELD_ORDER),
    # Phase 20 (20-01): сразу после «📝 Регистрация» — менеджер идёт по пути делегата
    # (анкета -> заявка), а не ищет «После одобрения» среди списков вариантов вопросов.
    ("📋 Заявки", "apps", _APPS_FIELD_ORDER),
    # Quick 260815-3hw: placed right after «📝 Регистрация» — a manager looks for tab names
    # near the registration settings, not buried at the tail of the settings list.
    ("📄 Вкладки таблицы", "sheets", _SHEETS_FIELD_ORDER),
    ("💳 Оплата", "pay", _PAY_FIELD_ORDER),
    ("🎉 Party", "party", _PARTY_FIELD_ORDER),
    ("📋 Согласия", "consent", _CONSENT_FIELD_ORDER),
    ("🎮 Геймификация", "game", _GAME_FIELD_ORDER),
    ("🔧 Система", "system", _SYSTEM_FIELD_ORDER),
]


def _settings_group_keys(token: str) -> list[str]:
    """Keys for a given SETTINGS_GROUPS token, including leftover-safety: any SETTINGS_FIELDS
    key not placed in a declared group lands in the trailing «Прочие»/"misc" group so nothing
    is ever silently hidden (mirrors _categorized_question_keys leftover handling)."""
    for _, tok, keys in SETTINGS_GROUPS:
        if tok == token:
            return list(keys)
    if token == "misc":
        seen = {k for _, __, keys in SETTINGS_GROUPS for k in keys}
        return [k for k, _, _ in SETTINGS_FIELDS if k not in seen]
    return []


def _settings_group_label(token: str) -> str:
    for label, tok, _ in SETTINGS_GROUPS:
        if tok == token:
            return label
    if token == "misc":
        return "📦 Прочие"
    return token


def _settings_nav_groups() -> list[tuple[str, str]]:
    """(label, token) rows for the landing keyboard nav buttons — declared groups plus a
    trailing «Прочие» group ONLY if leftover keys exist."""
    rows = [(label, tok) for label, tok, _ in SETTINGS_GROUPS]
    if _settings_group_keys("misc"):
        rows.append(("📦 Прочие", "misc"))
    return rows

# Phase 20 (20-04): в какой группе настроек живут фото и файлы — ОДИН факт в одном месте.
# Читают его и рендер экрана группы (обе ветки ниже), и обратный индекс
# `_group_of_setting_key`: разъехавшись, они отправили бы менеджера после загрузки фото на
# экран, где этой кнопки нет.
PHOTO_FILE_GROUP = "event"

PHOTO_FIELDS = [
    ("program", "📅 Программа", "Отправьте фото программы (можно с подписью)."),
    ("speakers", "🗣 Спикеры", "Отправьте одно фото со всеми спикерами (можно с подписью)."),
    ("start", "💬 Фото приветствия", "Отправьте фото для приветственного сообщения (/start)."),
    ("venue", "🏢 Площадка", "Отправьте фото площадки (можно с подписью)."),
]

FILE_FIELDS = [
    ("reg_bonus", "🎁 Бонус за регистрацию", "Отправьте файл или фото бонуса (можно с подписью)."),
]


def _group_of_setting_key(key: str) -> str | None:
    """Обратный индекс «ключ настройки -> токен группы», ВЫВЕДЕННЫЙ из SETTINGS_GROUPS
    (+ leftover-группа «misc»), а не второй литеральный словарь: он разъехался бы с
    группировкой при первой же перекладке ключа, и менеджер после правки значения уезжал бы
    на чужой экран. Нужен `handlers/admin_sections.py::settings_return_screen`, чтобы понять,
    с какого экрана менеджер правил значение.

    Композитный per-city ключ («{base}__city__{code}») приводится к базе — своей группы у
    городского значения нет, оно живёт в группе базового ключа.

    Медиа-ключи («{prefix}_photo_file_id» / «{prefix}_doc_file_id» / «{prefix}_caption») в
    SETTINGS_GROUPS не перечислены вовсе — их рисует ветка PHOTO_FIELDS/FILE_FIELDS экрана
    группы, поэтому и группа у них берётся из той же константы PHOTO_FILE_GROUP.

    Неизвестный ключ -> None (резолвер уйдёт на следующий шаг, тупика не будет)."""
    base = _base_setting_key(key)
    for _label, token, keys in SETTINGS_GROUPS:
        if base in keys:
            return token
    if base in _settings_group_keys("misc"):
        return "misc"
    for prefix, _label, _prompt in PHOTO_FIELDS + FILE_FIELDS:
        if base in (f"{prefix}_photo_file_id", f"{prefix}_doc_file_id", f"{prefix}_caption"):
            return PHOTO_FILE_GROUP
    return None


# Phase 09.3 (04, CITY-09): admin_id=None means "no header context" — reserved for tests
# and call sites where the admin is unknown; every production call site MUST pass the real
# admin id (structural test: tests/test_regmode_header_093.py asserts no empty-parens call
# of render_settings_text/build_settings_keyboard remains in this file).
#
# Phase 20 (20-04): ЭТА ФУНКЦИЯ БОЛЬШЕ НЕ ЯВЛЯЕТСЯ ЭКРАНОМ. Плоский лендинг настроек заменён
# восемью разделами (handlers/admin_sections.py), ни один хендлер её не рисует. Она и её пара
# `build_settings_keyboard` сохранены как снапшот-контракт раскладки тумблеров
# (tests/test_settings_groups_c0x.py::test_settings_toggle_button_snapshot) и как потребитель
# `settings_toggle_rows` — не мёртвый код, удалять нельзя.
async def render_settings_text(admin_id: int | None = None) -> str:
    # Phase 09.3 (04, CITY-09): WR-05 — resolve the header ONCE for this whole render call.
    # `admin_id is None` (tests, unknown-caller sites) and `header_code in (None, ALL_CITIES)`
    # (module off / no choice yet / explicit «все города») all collapse to the SAME branch
    # below — byte-identical to pre-phase output (CONTEXT D module-off parity).
    header_code = await admin_selected_city(admin_id) if admin_id is not None else None
    per_city_ctx = bool(header_code and header_code != ALL_CITIES)

    lines = []
    if per_city_ctx:
        lines.append(f"🏙 {html_module.escape(await city_label(header_code))}")
    lines += ["⚙️ <b>Настройки форума</b>", ""]

    if per_city_ctx:
        # T-093-13: composed key comes ONLY from cities.per_city_key (closed-set guard).
        reg_mode = await get_setting_typed_for_city("registration_mode", header_code)
        mode_label = "📋 Полная" if reg_mode == "full" else "⚡ Краткая"
        own_key = per_city_key("registration_mode", header_code)
        own_mark = " — своё" if (own_key and await get_setting(own_key)) else " — как везде"
        lines.append(f"📝 Форма регистрации: <b>{mode_label}</b>{own_mark}")
    else:
        # REG-02 (06-07): final-coverage sweep — closes the 06-05-flagged boundary; byte-
        # identical to the prior `get_setting(k) or "<literal>"` idiom (enum falsy->default).
        reg_mode = await get_setting_typed("registration_mode")
        mode_label = "📋 Полная" if reg_mode == "full" else "⚡ Краткая"
        lines.append(f"📝 Форма регистрации: <b>{mode_label}</b>")

    # REG-02 (06-05): feature-switch reads resolved via the registry's enum default,
    # byte-identical to the prior `get_setting(k) or "<literal>"` idiom (get_setting_typed's
    # enum branch is `raw if raw else default`, matching falsy-to-default on empty-string).
    bonus_enabled = await get_setting_typed("reg_bonus_enabled")
    bonus_label = "✅ Вкл" if bonus_enabled == "on" else "❌ Выкл"
    lines.append(f"🎁 Бонус за регистрацию: <b>{bonus_label}</b>")

    full_appr = await get_setting_typed("full_approval")
    short_appr = await get_setting_typed("short_approval")
    notify_mode = await get_setting_typed("pending_notify_mode")
    appr_lbl = lambda v: "👮 Ручная" if v == "manual" else "⚡ Авто"
    lines.append(f"✅ Модерация полной формы: <b>{appr_lbl(full_appr)}</b>")
    lines.append(f"✅ Модерация краткой формы: <b>{appr_lbl(short_appr)}</b>")
    notify_lbl = "📨 Сразу" if notify_mode == "instant" else "🕒 Пачкой (напоминалка)"
    lines.append(f"🔔 Уведомление о заявке: <b>{notify_lbl}</b>")

    payment_enabled = await get_setting_typed("payment_enabled")
    consent_enabled = await get_setting_typed("consent_enabled")
    lines.append(f"💳 Модуль оплаты: <b>{'✅ Вкл' if payment_enabled == 'on' else '❌ Выкл'}</b>")
    lines.append(f"📋 Модуль согласий: <b>{'✅ Вкл' if consent_enabled == 'on' else '❌ Выкл'}</b>")
    pay_rem_enabled = await get_setting_typed("payment_reminders_enabled")
    lines.append(f"⏰ Автонапоминания об оплате: <b>{'✅ Вкл' if pay_rem_enabled == 'on' else '❌ Выкл'}</b>")

    # Phase 5 (D-13): party settings always read as off/manual when unset — new-capability
    # default-OFF posture (Phase-4 D-15 lineage), independent of full_approval/short_approval.
    party_enabled = await get_setting_typed("party_enabled")
    party_fork_question = await get_setting_typed("party_fork_question")
    party_approval = await get_setting_typed("party_approval")
    lines.append(f"🎉 Трек вечеринки: <b>{'✅ Вкл' if party_enabled == 'on' else '❌ Выкл'}</b>")
    lines.append(f"🔀 Вопрос-развилка формата: <b>{'✅ Вкл' if party_fork_question == 'on' else '❌ Выкл'}</b>")
    lines.append(f"✅ Модерация вечеринки: <b>{appr_lbl(party_approval)}</b>")
    # Предотбор по Google-таблице (VERIF-01/02): enum on/off, дефолт "off" из реестра.
    preselect_enabled = await get_setting_typed("preselect_enabled")
    lines.append(f"🎯 Предотбор по таблице: <b>{'✅ Вкл' if preselect_enabled == 'on' else '❌ Выкл'}</b>")
    pending_rem = await get_setting_typed("pending_reminder_enabled")
    nudge_on = await get_setting_typed("nudge_enabled")
    lines.append(f"📋 Сводка о заявках в ожидании: <b>{'✅ Вкл' if pending_rem == 'on' else '❌ Выкл'}</b>")
    lines.append(f"⏰ Догонялка брошенных анкет: <b>{'✅ Вкл' if nudge_on == 'on' else '❌ Выкл'}</b>")

    enabled_q = 0
    for _, sk, *_rest in REG_FLOW:
        # REG-02 (06-04): resolves via the registry's toggle default, byte-identical to the
        # prior REG_DEFAULTS.get(sk, "on") == "on" fallback (get_setting_typed's toggle
        # branch is (raw == "on") if raw is not None else (default == "on")).
        is_on = await get_setting_typed(sk)
        if is_on:
            enabled_q += 1
    lines.append(f"📋 Вопросы: <b>{enabled_q} из {len(REG_FLOW)}</b> включено")

    enabled_m = 0
    for key, _ in MENU_BUTTONS:
        if per_city_ctx:
            # Effective per-city resolver (fallback to global) — CONTEXT B: the counter at
            # the header's city must reflect what that city's delegates actually see.
            is_on = await get_setting_typed_for_city(key, header_code) == "on"
        else:
            v = await get_setting(key)
            is_on = (v == "on") if v is not None else True
        if is_on:
            enabled_m += 1
    lines.append(f"🔘 Меню: <b>{enabled_m} из {len(MENU_BUTTONS)}</b> кнопок")
    lines.append("")

    lines.append("✏️ Тексты и медиа — по кнопкам групп ниже.")

    lines.append("")
    lines.append("<i>Отправьте «-» при редактировании текстовых полей, чтобы скрыть.</i>")
    return "\n".join(lines)


# Phase 20 (20-01, ADMIN-IA-01): ЕДИНСТВЕННЫЙ источник строк-тумблеров настроек. Отсюда их
# берёт и старый лендинг (`build_settings_keyboard` ниже), и экраны разделов
# (`handlers/admin_sections.py`) — иначе один и тот же тумблер имел бы две разные подписи в
# двух местах. Ключ словаря — callback_data тумблера; значение — СТРОКИ клавиатуры (список
# списков кнопок): у `settings_toggle_reg` их две, когда у города шапки есть собственное
# значение registration_mode и показывается «↩️ Как везде», иначе одна.
#
# Phase 09.3 (04, CITY-09): WR-05 — шапка города читается РОВНО ОДИН раз за вызов, всё
# остальное (per_city_ctx, own_key) выводится из этого единственного чтения.
async def settings_toggle_rows(admin_id: int | None = None) -> dict[str, list[list[InlineKeyboardButton]]]:
    header_code = await admin_selected_city(admin_id) if admin_id is not None else None
    per_city_ctx = bool(header_code and header_code != ALL_CITIES)

    # REG-02 (06-05): feature-switch reads resolved via the registry's enum default,
    # byte-identical to the prior `get_setting(k) or "<literal>"` idiom — button TEXT
    # ternaries and callback_data strings are intentionally untouched (D-12).
    if per_city_ctx:
        reg_mode = await get_setting_typed_for_city("registration_mode", header_code)
    else:
        reg_mode = await get_setting_typed("registration_mode")
    toggle_text = "📝 Регистрация: ⚡ Краткая → 📋 Полная" if reg_mode == "short" else "📝 Регистрация: 📋 Полная → ⚡ Краткая"

    bonus_enabled = await get_setting_typed("reg_bonus_enabled")
    bonus_toggle_text = "🎁 Бонус: ❌ Выкл → ✅ Вкл" if bonus_enabled == "off" else "🎁 Бонус: ✅ Вкл → ❌ Выкл"

    full_appr = await get_setting_typed("full_approval")
    short_appr = await get_setting_typed("short_approval")
    notify_mode = await get_setting_typed("pending_notify_mode")
    full_txt = "✅ Полная форма: 👮 Ручная → ⚡ Авто" if full_appr == "manual" else "✅ Полная форма: ⚡ Авто → 👮 Ручная"
    short_txt = "✅ Краткая форма: 👮 Ручная → ⚡ Авто" if short_appr == "manual" else "✅ Краткая форма: ⚡ Авто → 👮 Ручная"
    notify_txt = "🔔 Уведомление: 📨 Сразу → 🕒 Пачкой" if notify_mode == "instant" else "🔔 Уведомление: 🕒 Пачкой → 📨 Сразу"

    payment_enabled = await get_setting_typed("payment_enabled")
    consent_enabled = await get_setting_typed("consent_enabled")
    payment_toggle_text = "💳 Оплата: ❌ Выкл → ✅ Вкл" if payment_enabled != "on" else "💳 Оплата: ✅ Вкл → ❌ Выкл"
    consent_toggle_text = "📋 Согласия: ❌ Выкл → ✅ Вкл" if consent_enabled != "on" else "📋 Согласия: ✅ Вкл → ❌ Выкл"
    pay_rem_enabled = await get_setting_typed("payment_reminders_enabled")
    pay_rem_toggle_text = ("⏰ Автонапоминания оплаты: ✅ Вкл → ❌ Выкл" if pay_rem_enabled == "on"
                           else "⏰ Автонапоминания оплаты: ❌ Выкл → ✅ Вкл")

    uni_mode = await get_setting_typed("reg_university_mode")
    uni_mode_text = ("🏫 ВУЗ: выбор из списка → свободный ввод" if uni_mode == "list"
                     else "🏫 ВУЗ: свободный ввод → выбор из списка")
    edu_cond = await get_setting_typed("edu_conditional")
    edu_cond_text = ("🎓 ВУЗ/курс только у студентов: ✅ Вкл → ❌ Выкл" if edu_cond == "on"
                     else "🎓 ВУЗ/курс только у студентов: ❌ Выкл → ✅ Вкл")
    show_progress = await get_setting_typed("reg_show_progress")
    show_progress_text = ("🔢 Нумерация вопросов: ✅ Вкл → ❌ Выкл" if show_progress == "on"
                          else "🔢 Нумерация вопросов: ❌ Выкл → ✅ Вкл")

    # Phase 5 (D-13): party_enabled / party_fork_question default OFF; party_approval
    # default "manual" — resolved via the registry's enum default (REG-02, 06-05).
    party_enabled = await get_setting_typed("party_enabled")
    party_toggle_text = ("🎉 Трек вечеринки: ❌ Выкл → ✅ Вкл" if party_enabled != "on"
                         else "🎉 Трек вечеринки: ✅ Вкл → ❌ Выкл")
    party_fork_question = await get_setting_typed("party_fork_question")
    party_fork_toggle_text = ("🔀 Вопрос-развилка формата: ❌ Выкл → ✅ Вкл" if party_fork_question != "on"
                              else "🔀 Вопрос-развилка формата: ✅ Вкл → ❌ Выкл")
    party_approval = await get_setting_typed("party_approval")
    party_appr_txt = ("✅ Модерация вечеринки: 👮 Ручная → ⚡ Авто" if party_approval == "manual"
                      else "✅ Модерация вечеринки: ⚡ Авто → 👮 Ручная")
    preselect_enabled = await get_setting_typed("preselect_enabled")
    preselect_toggle_text = ("🎯 Предотбор по таблице: ❌ Выкл → ✅ Вкл" if preselect_enabled != "on"
                             else "🎯 Предотбор по таблице: ✅ Вкл → ❌ Выкл")
    pending_rem = await get_setting_typed("pending_reminder_enabled")
    pending_rem_text = ("📋 Сводка о заявках: ✅ Вкл → ❌ Выкл" if pending_rem == "on"
                        else "📋 Сводка о заявках: ❌ Выкл → ✅ Вкл")
    nudge_on = await get_setting_typed("nudge_enabled")
    nudge_toggle_text = ("⏰ Догонялка анкет: ✅ Вкл → ❌ Выкл" if nudge_on == "on"
                         else "⏰ Догонялка анкет: ❌ Выкл → ✅ Вкл")

    reg_rows = [[InlineKeyboardButton(text=toggle_text, callback_data="settings_toggle_reg")]]
    # Phase 09.3 (04, CITY-09): registration_mode has no settings_edit:{key} screen of its
    # own (it's a landing toggle, not a SETTINGS_FIELDS text entry) — the header toggle above
    # IS its per-city editor now; the old picker shortcut («🏙 Форма по городам», entering
    # the per-key city picker screen) is gone. Reset row only when the header's city has an
    # own override to reset (same "↩️ Как везде only when something to undo" idiom the
    # header-scoped per-key editor uses below — never shown with nothing to undo).
    if per_city_ctx:
        own_key = per_city_key("registration_mode", header_code)
        if own_key and await get_setting(own_key):
            reg_rows.append([InlineKeyboardButton(text="↩️ Как везде", callback_data="settings_regmode_reset")])

    def _row(text: str, callback_data: str) -> list[list[InlineKeyboardButton]]:
        return [[InlineKeyboardButton(text=text, callback_data=callback_data)]]

    return {
        "settings_toggle_reg": reg_rows,
        "settings_toggle_bonus": _row(bonus_toggle_text, "settings_toggle_bonus"),
        "settings_toggle_full_approval": _row(full_txt, "settings_toggle_full_approval"),
        "settings_toggle_short_approval": _row(short_txt, "settings_toggle_short_approval"),
        "settings_toggle_notify": _row(notify_txt, "settings_toggle_notify"),
        "toggle_payment_enabled": _row(payment_toggle_text, "toggle_payment_enabled"),
        "toggle_payment_reminders": _row(pay_rem_toggle_text, "toggle_payment_reminders"),
        "toggle_consent_enabled": _row(consent_toggle_text, "toggle_consent_enabled"),
        "toggle_uni_mode": _row(uni_mode_text, "toggle_uni_mode"),
        "toggle_edu_conditional": _row(edu_cond_text, "toggle_edu_conditional"),
        "toggle_show_progress": _row(show_progress_text, "toggle_show_progress"),
        "toggle_party_enabled": _row(party_toggle_text, "toggle_party_enabled"),
        "toggle_party_fork_question": _row(party_fork_toggle_text, "toggle_party_fork_question"),
        "settings_toggle_party_approval": _row(party_appr_txt, "settings_toggle_party_approval"),
        "toggle_preselect_enabled": _row(preselect_toggle_text, "toggle_preselect_enabled"),
        "toggle_pending_reminder": _row(pending_rem_text, "toggle_pending_reminder"),
        "toggle_nudge_enabled": _row(nudge_toggle_text, "toggle_nudge_enabled"),
    }


# Phase 09.3 (04, CITY-09): admin_id=None means "no header context" — see the comment
# above render_settings_text (same contract, same structural test).
#
# Phase 20 (20-04): как и её пара выше, ПОСЛЕ ФАЗЫ 20 НЕ ЯВЛЯЕТСЯ ЭКРАНОМ — сохранена как
# снапшот-контракт раскладки тумблеров и единственный оставшийся потребитель
# `settings_toggle_rows`. Ни одна перерисовка после действия менеджера сюда не целится:
# сторож tests/test_admin_sections_ia20.py требует ровно одного упоминания на весь handlers/ —
# вот этого объявления.
async def build_settings_keyboard(admin_id: int | None = None):
    # Phase 20 (20-01): порядок строк лендинга прежний, байт-в-байт; сами кнопки приходят из
    # settings_toggle_rows (WR-05: шапка города прочитана там ровно один раз).
    rows = await settings_toggle_rows(admin_id)

    buttons: list[list[InlineKeyboardButton]] = []
    for cb in ("settings_toggle_reg", "settings_toggle_bonus", "settings_toggle_full_approval",
               "settings_toggle_short_approval", "settings_toggle_notify",
               "toggle_payment_enabled", "toggle_payment_reminders", "toggle_consent_enabled"):
        buttons += rows[cb]
    buttons.append([InlineKeyboardButton(text="🧾 PDF согласий", callback_data="admin_consent_pdfs")])
    for cb in ("toggle_uni_mode", "toggle_edu_conditional", "toggle_show_progress",
               "toggle_party_enabled", "toggle_party_fork_question",
               "settings_toggle_party_approval", "toggle_preselect_enabled",
               "toggle_pending_reminder", "toggle_nudge_enabled"):
        buttons += rows[cb]
    buttons += [
        [InlineKeyboardButton(text="🎛 Тип события (пресет)", callback_data="admin_event_preset")],
        [InlineKeyboardButton(text="📋 Вопросы регистрации", callback_data="admin_reg_questions")],
        [InlineKeyboardButton(text="✏️ Тексты вопросов", callback_data="admin_reg_prompts")],
        [InlineKeyboardButton(text="🔘 Кнопки меню", callback_data="admin_menu_buttons")],
        [InlineKeyboardButton(text="👥 Роли и доступы", callback_data="admin_roles")],
        [InlineKeyboardButton(text="📊 Дашборд", callback_data="admin_dashboard_settings")],
        [InlineKeyboardButton(text="🎨 Оформление", callback_data="admin_miniapp_settings")],
    ]
    for label, token in _settings_nav_groups():
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"settings_group:{token}")])
    buttons.append([InlineKeyboardButton(text="← Назад", callback_data="settings_back")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def render_settings_group_text(token: str, admin_id: int | None = None) -> str:
    """Quick 260724-c0x: per-group sub-screen — status FLAGS only («задано»/«не задано»/
    «по умолчанию»), never the raw value inline (that stays behind the existing
    settings_edit tap-through, unchanged).

    Phase 09.3 (05, CITY-09): WR-05 — resolve the header ONCE for this whole render call.
    `admin_id is None` (tests, unknown-caller sites) and `header_code in (None, ALL_CITIES)`
    (module off / explicit «все города») all collapse to the SAME branch below —
    byte-identical to pre-phase output (CONTEXT D module-off parity)."""
    header_code = await admin_selected_city(admin_id) if admin_id is not None else None
    per_city_ctx = bool(header_code and header_code != ALL_CITIES)

    group_label = _settings_group_label(token)
    lines = []
    if per_city_ctx:
        lines.append(f"🏙 {html_module.escape(await city_label(header_code))}")
    lines += [f"⚙️ <b>Настройки → {group_label}</b>", ""]

    field_labels = {k: lbl for k, lbl, _ in SETTINGS_FIELDS}
    # Phase 09.2 (C, CITY-05): compact «🏙 N» override-count marker, only when the cities
    # module is on — module off = byte-identical to today's text (CONTEXT C). The full list
    # of city names lives on the per-key editor screen (settings_edit callback family), not
    # here — this screen deliberately never shows raw values inline (quick 260724-c0x contract).
    city_module_on = await cities_module_on()
    for key in _settings_group_keys(token):
        label = field_labels.get(key, key)
        if per_city_ctx and is_per_city(key):
            # Phase 09.3 (05, CITY-09, CONTEXT B): flag relative to the header's city —
            # «✏️ своё» if the city has its own value, else «как везде · {общее}» using the
            # SAME ladder as the global branch below (no duplicated wording rules).
            own_key = per_city_key(key, header_code)
            if own_key and await get_setting(own_key):
                flag = "✏️ своё"
            else:
                value = await get_setting(key)
                if value:
                    common = "задано"
                elif key in _SETTINGS_DISPLAY_DEFAULTS:
                    common = "по умолчанию"
                else:
                    common = "не задано"
                flag = f"как везде · {common}"
            lines.append(f"{label}: {flag}")
            continue
        value = await get_setting(key)
        if value:
            flag = "✏️ задано"
        elif key in _SETTINGS_DISPLAY_DEFAULTS:
            flag = "<i>по умолчанию</i>"
        else:
            flag = "<i>— не задано</i>"
        city_suffix = ""
        if city_module_on and is_per_city(key):
            codes = await city_override_codes(key)
            if codes:
                city_suffix = f" · 🏙 {len(codes)}"
        lines.append(f"{label}: {flag}{city_suffix}")

    if token == "consent": lines += await consent_group_extra_lines()  # quick 260822 (шов admin_consent)
    if token == PHOTO_FILE_GROUP:
        for prefix, label, _ in PHOTO_FIELDS:
            photo = await get_setting(f"{prefix}_photo_file_id")
            lines.append(f"{label}: {'✅ загружена' if photo else '<i>— не задано</i>'}")
        for prefix, label, _ in FILE_FIELDS:
            photo = await get_setting(f"{prefix}_photo_file_id")
            doc = await get_setting(f"{prefix}_doc_file_id")
            lines.append(f"{label}: {'✅ загружен' if (photo or doc) else '<i>— не задано</i>'}")

    return "\n".join(lines)


async def build_settings_group_keyboard(token: str, admin_id: int | None = None):
    """Reuses the existing settings_edit/settings_photo/settings_file callbacks unchanged —
    only the button placement changes. Configured fields first, then a noop section-header
    button (req #2: collapse unconfigured fields), then unconfigured fields.

    Phase 09.3 (05, CITY-09): WR-05 — resolve the header ONCE, same contract as
    render_settings_group_text above (kept as a second read here, not shared across the two
    functions, since they're independent render calls per call site — matches the
    render_settings_text/build_settings_keyboard precedent from plan 04)."""
    header_code = await admin_selected_city(admin_id) if admin_id is not None else None
    per_city_ctx = bool(header_code and header_code != ALL_CITIES)

    field_labels = {k: lbl for k, lbl, _ in SETTINGS_FIELDS}
    configured: list[InlineKeyboardButton] = []
    unconfigured: list[InlineKeyboardButton] = []

    for key in _settings_group_keys(token):
        label = field_labels.get(key, key)
        btn = InlineKeyboardButton(text=f"✏️ {label}", callback_data=f"settings_edit:{key}")
        if per_city_ctx and is_per_city(key):
            # CONTEXT B: «настроено» = effective value at the header's city (своё, иначе
            # общее) — a key with only a city override must land in «настроено», not the
            # collapsed «не настроено» section.
            own_key = per_city_key(key, header_code)
            own_value = await get_setting(own_key) if own_key else None
            value = own_value or await get_setting(key)
        else:
            value = await get_setting(key)
        (configured if value else unconfigured).append(btn)

    if token == PHOTO_FILE_GROUP:
        for prefix, label, _ in PHOTO_FIELDS:
            btn = InlineKeyboardButton(text=f"📷 {label}", callback_data=f"settings_photo:{prefix}")
            photo = await get_setting(f"{prefix}_photo_file_id")
            (configured if photo else unconfigured).append(btn)
        for prefix, label, _ in FILE_FIELDS:
            btn = InlineKeyboardButton(text=f"📎 {label}", callback_data=f"settings_file:{prefix}")
            photo = await get_setting(f"{prefix}_photo_file_id")
            doc = await get_setting(f"{prefix}_doc_file_id")
            (configured if (photo or doc) else unconfigured).append(btn)

    buttons = [[b] for b in configured]
    if unconfigured:
        buttons.append([InlineKeyboardButton(text="── не настроено ──", callback_data="settings_group_noop")])
        buttons.extend([[b] for b in unconfigured])
    if token == "game":  # Quick 260822: режим уведомлений о сдачах — тумблер, не ввод кода
        buttons.append([InlineKeyboardButton(text=await game_submit_notify_button_text(), callback_data="toggle_game_submit_notify")])
    # Phase 20 (20-01): «🔄 Новый сезон» и «📥 Импорт прошлого события» съехали с экрана
    # группы «🎪 Событие/Медиа» в раздел «🔧 Управление» (handlers/admin_sections.py) — это
    # операции над всем событием, а не тексты и медиа. Условие суперадмина для «Нового
    # сезона» переехало вместе с кнопкой (тип строки `screen_admin` в реестре разделов);
    # настоящий гейт — прежняя перепроверка config.ADMIN_IDS внутри самих хендлеров визарда,
    # потому что стейл-клавиатура в чате живёт вечно.
    if token == "consent": buttons += consent_group_extra_buttons()  # quick 260822 (шов admin_consent)
    # Phase 20 (20-04): «Назад» с экрана группы ведёт в РАЗДЕЛ-владелец этой группы
    # («🎪 Событие/Медиа» -> «🎪 Событие», «📋 Заявки» -> «📋 Заявки»), а не на исчезнувший
    # плоский лендинг. Цель считает `section_of` из реестра SECTIONS — второй карты нет.
    from handlers.admin_sections import back_button  # ленивый шов (цикл на уровне модуля)
    buttons.append([back_button(f"settings_group:{token}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data == "admin_settings")
async def show_admin_settings(callback: types.CallbackQuery):
    """D-03, совместимость стейл-клавиатур: на этот callback ссылаются десятки «← Назад» из
    клавиатур, отрисованных ДО фазы 20 и живущих в чатах вечно. Плоского лендинга больше нет,
    поэтому кнопка приземляется на КОРЕНЬ разделов с объяснением переезда — тупика и
    необработанного callback'а не возникает."""
    from handlers.admin_sections import settings_return_screen  # ленивый шов
    text, kb = await settings_return_screen(callback.from_user.id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("settings_group:"))
async def show_settings_group(callback: types.CallbackQuery):
    token = callback.data.split(":", 1)[1]
    text = await render_settings_group_text(token, callback.from_user.id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_settings_group_keyboard(token, callback.from_user.id))
    await callback.answer()


@router.callback_query(F.data == "settings_group_noop")
async def settings_group_noop(callback: types.CallbackQuery):
    # Section-header button in the collapsed «не настроено» view — not actionable.
    await callback.answer()


@router.callback_query(F.data == "settings_toggle_reg")
async def toggle_registration_mode(callback: types.CallbackQuery):
    # Phase 09.3 (04, CITY-09): WR-05 — single header read for this handler.
    admin_id = callback.from_user.id
    header_code = await admin_selected_city(admin_id)
    if header_code and header_code != ALL_CITIES:
        # T-093-12: re-check the RIGHT in the handler, not just via a hidden button.
        visible = await _per_city_visible_codes(admin_id)
        if header_code not in visible:
            await callback.answer("Этот город правит суперадмин", show_alert=True)
            return
        # T-093-13: composed key comes ONLY from cities.per_city_key.
        composed = per_city_key("registration_mode", header_code)
        if composed is None:
            await callback.answer("Неизвестный город", show_alert=True)
            return
        current = await get_setting_typed_for_city("registration_mode", header_code)
        new_mode = "full" if current == "short" else "short"
        await set_setting(composed, new_mode)
        city_txt = await city_label(header_code)
        human = _enum_human_label("registration_mode", new_mode)
        await callback.answer(f"Форма регистрации для {city_txt}: {human}", show_alert=True)
    else:
        # REG-02 (06-05): current-value read migrated to the registry; write path unchanged.
        current = await get_setting_typed("registration_mode")
        new_mode = "full" if current == "short" else "short"
        await set_setting("registration_mode", new_mode)
        label = "📋 Полная" if new_mode == "full" else "⚡ Краткая"
        await callback.answer(f"Форма регистрации: {label}", show_alert=True)

    # Phase 20 (20-04): тумблер перерисовывает РАЗДЕЛ, с которого его нажали. Раздел не
    # задаётся, а выводится: `callback.data` тумблера и есть строка ("toggle", …) реестра
    # SECTIONS, `section_of` найдёт владельца — словаря «тумблер -> раздел» нет.
    from handlers.admin_sections import settings_return_screen  # ленивый шов
    text, kb = await settings_return_screen(admin_id, callback_data=callback.data)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    # Phase 7 (SHORT-03, gate #5): materialize the short tab the moment the manager flips
    # into "Краткая" — no need to wait for the first registration. Switching back to "Полная"
    # is a no-op (the gate inside returns early); the tab and its data are never touched.
    # Materializing the tab is a property of the EVENT ("Акция"), not the city — always run,
    # even when this toggle just wrote a per-city override.
    # Phase 13 (13-05): _refresh_short_sheet_header moved to admin_reg_config.py; local import
    # (same idiom _refresh_party_sheet_header/_refresh_short_sheet_header themselves use for
    # handlers.registration) avoids triggering admin_reg_config's own back-import of this
    # module before admin.py has finished defining the names admin_reg_config imports back.
    from handlers.admin_reg_config import _refresh_short_sheet_header
    await _refresh_short_sheet_header()


@router.callback_query(F.data == "settings_regmode_reset")
async def settings_regmode_reset(callback: types.CallbackQuery):
    """Confirm screen for «↩️ Как везде» on the header-scoped registration-mode toggle —
    same two-step confirm gate idiom as the header-scoped per-key editor's own reset pair
    below (settings_reset_city/settings_reset_city_go)."""
    admin_id = callback.from_user.id
    header_code = await admin_selected_city(admin_id)
    if not header_code or header_code == ALL_CITIES:
        await callback.answer("Нет своего значения для сброса", show_alert=True)
        return
    visible = await _per_city_visible_codes(admin_id)
    if header_code not in visible:
        await callback.answer("Этот город правит суперадмин", show_alert=True)
        return
    composed = per_city_key("registration_mode", header_code)
    if composed is None or not await get_setting(composed):
        await callback.answer("Нет своего значения для сброса", show_alert=True)
        return

    city_txt = await city_label(header_code)
    global_value = await get_setting_typed("registration_mode")
    global_human = _enum_human_label("registration_mode", global_value)
    text = (
        f"Город {html_module.escape(city_txt)} снова будет использовать общую форму "
        f"регистрации:\n<b>{global_human}</b>\n\nСвоя форма регистрации города будет удалена."
    )
    # Phase 20 (20-04): отмена возвращает в «📝 Анкета» — раздел тумблера `settings_toggle_reg`,
    # чья условная вложенная строка «↩️ Как везде» и открыла этот экран (своей записи в
    # SECTIONS у экрана подтверждения нет и быть не должно). Подпись остаётся «← Отмена»:
    # это отказ от действия, а не навигация.
    from handlers.admin_sections import back_button  # ленивый шов
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, как везде", callback_data=f"settings_regmode_reset_go:{header_code}")],
        [back_button("settings_toggle_reg", text="← Отмена")],
    ])
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("settings_regmode_reset_go:"))
async def settings_regmode_reset_go(callback: types.CallbackQuery):
    # Phase 20 (20-04): обе перерисовки ниже (отказ по свежести и успешный сброс) возвращают
    # в «📝 Анкета». Подсказка — `settings_toggle_reg`, а НЕ собственный callback хендлера:
    # `settings_regmode_reset_go:{code}` в SECTIONS не объявлен, кнопка «↩️ Как везде» —
    # условная вложенная строка тумблера регистрации, и раздел у неё тот же.
    from handlers.admin_sections import settings_return_screen  # ленивый шов
    admin_id = callback.from_user.id
    code = callback.data.split(":", 1)[1]
    # Fail-closed (RESEARCH Pattern 2 / review WR-02): module off never deletes a per-city
    # override, even on a forged callback — the same explicit local guard every sibling
    # reset/edit write handler has (settings_reset_city_go, menu_reset_city_go), instead of
    # relying implicitly on admin_selected_city() returning None when the module is off.
    if not await cities_module_on():
        await callback.answer("Города выключены", show_alert=True)
        return

    # T-093-13: composed key comes ONLY from cities.per_city_key — refuse on an unknown code
    # before touching rights or freshness (mirrors settings_reset_city_go's own guard order).
    composed = per_city_key("registration_mode", code)
    if composed is None:
        await callback.answer("Неизвестный город", show_alert=True)
        return
    # T-093-12: RIGHT check against the code carried in callback_data (not just the current
    # header) — this is what catches a bound manager's forged confirmation for another city,
    # since the freshness check below alone could never distinguish "forged" from "stale"
    # (a bound manager's OWN header can never actually become another city).
    visible = await _per_city_visible_codes(admin_id)
    if code not in visible:
        await callback.answer("Этот город правит суперадмин", show_alert=True)
        return
    # T-093-14: freshness — the confirm screen named the header's city; if the header moved
    # on since, refuse and make the admin re-open the confirm screen for the NEW city.
    current = await admin_selected_city(admin_id)
    if code != current:
        await callback.answer("Город админки изменился — подтвердите заново.", show_alert=True)
        text, kb = await settings_return_screen(admin_id, callback_data="settings_toggle_reg")
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        return

    await delete_setting(composed)  # idempotent — safe if already absent
    city_txt = await city_label(code)
    await callback.answer(f"Готово: {city_txt} — как везде", show_alert=True)
    text, kb = await settings_return_screen(admin_id, callback_data="settings_toggle_reg")
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


async def _toggle_approval_setting(callback: types.CallbackQuery, key: str, default: str, title: str):
    # REG-02 (06-07): final-coverage sweep — key is always in SETTINGS_SCHEMA (full_approval/
    # short_approval/party_approval), registry default byte-identical to the `default` param.
    current = await get_setting_typed(key)
    new_val = "auto" if current == "manual" else "manual"
    await set_setting(key, new_val)
    await callback.answer(f"{title}: {'👮 Ручная' if new_val == 'manual' else '⚡ Авто'}", show_alert=True)
    # Phase 20 (20-04): одна правка на generic-хелпер покрывает все его callback'и — раздел
    # выводится из `callback.data` через SECTIONS, а не задаётся словарём (см. комментарий
    # у toggle_registration_mode).
    from handlers.admin_sections import settings_return_screen  # ленивый шов
    text, kb = await settings_return_screen(callback.from_user.id, callback_data=callback.data)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data == "settings_toggle_full_approval")
async def toggle_full_approval(callback: types.CallbackQuery):
    await _toggle_approval_setting(callback, "full_approval", "manual", "Модерация полной формы")


@router.callback_query(F.data == "settings_toggle_short_approval")
async def toggle_short_approval(callback: types.CallbackQuery):
    await _toggle_approval_setting(callback, "short_approval", "auto", "Модерация краткой формы")


@router.callback_query(F.data == "settings_toggle_party_approval")
async def toggle_party_approval(callback: types.CallbackQuery):
    # D-13: independent setting — never reads/writes/derives from full_approval or
    # short_approval, no fallback chain between them.
    await _toggle_approval_setting(callback, "party_approval", "manual", "Модерация вечеринки")


# ── Phase 4: module on/off toggles (payment, consent) + event-type preset ────

async def _toggle_module_setting(callback: types.CallbackQuery, key: str, title: str):
    """On/off toggle for a Phase 4 module flag (fail-safe default OFF, D-15)."""
    # REG-02 (06-07): final-coverage sweep — key is always in SETTINGS_SCHEMA
    # (payment_enabled/consent_enabled/party_enabled/party_fork_question), all default "off".
    current = await get_setting_typed(key)
    new_val = "off" if current == "on" else "on"
    await set_setting(key, new_val)
    label = "✅ Вкл" if new_val == "on" else "❌ Выкл"
    await callback.answer(f"{title}: {label}", show_alert=True)
    from handlers.admin_sections import settings_return_screen  # ленивый шов (20-04)
    text, kb = await settings_return_screen(callback.from_user.id, callback_data=callback.data)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await remind_consent_purposes_if_widened(callback.message, key, new_val)


@router.callback_query(F.data == "toggle_payment_enabled")
async def toggle_payment_enabled(callback: types.CallbackQuery):
    await _toggle_module_setting(callback, "payment_enabled", "💳 Оплата")


@router.callback_query(F.data == "toggle_consent_enabled")
async def toggle_consent_enabled(callback: types.CallbackQuery):
    await _toggle_module_setting(callback, "consent_enabled", "📋 Согласия")


@router.callback_query(F.data == "toggle_party_enabled")
async def toggle_party_enabled(callback: types.CallbackQuery):
    # D-11a master gate: default OFF (fail-safe, Phase-4 D-15 lineage).
    await _toggle_module_setting(callback, "party_enabled", "🎉 Трек вечеринки")


@router.callback_query(F.data == "toggle_party_fork_question")
async def toggle_party_fork_question(callback: types.CallbackQuery):
    # D-10: default OFF — an ordinary delegate sees no extra screen until an admin opts in.
    await _toggle_module_setting(callback, "party_fork_question", "🔀 Вопрос-развилка формата")


@router.callback_query(F.data == "toggle_preselect_enabled")
async def toggle_preselect_enabled(callback: types.CallbackQuery):
    # Гейт предотбора по Google-таблице: enum on/off, дефолт OFF — без таблицы новичок
    # регистрируется как обычно. Тот же generic-хелпер, что и у соседей-модулей.
    await _toggle_module_setting(callback, "preselect_enabled", "🎯 Предотбор по таблице")


@router.callback_query(F.data == "toggle_pending_reminder")
async def toggle_pending_reminder(callback: types.CallbackQuery):
    # Сводка «Заявок в ожидании: N» админам (services/reminders.py): enum on/off, дефолт ON.
    await _toggle_module_setting(callback, "pending_reminder_enabled", "📋 Сводка о заявках")


@router.callback_query(F.data == "toggle_nudge_enabled")
async def toggle_nudge_enabled(callback: types.CallbackQuery):
    # Догонялка брошенных анкет (services/scheduler.py): enum on/off, дефолт ON.
    await _toggle_module_setting(callback, "nudge_enabled", "⏰ Догонялка анкет")


@router.callback_query(F.data == "toggle_payment_reminders")
async def toggle_payment_reminders(callback: types.CallbackQuery):
    # Default ON — preserves prior behaviour (reminders fired whenever a deadline was set).
    await _toggle_value_setting(
        callback, "payment_reminders_enabled", "on", "off", "on",
        "⏰ Автонапоминания об оплате включены", "⏰ Автонапоминания об оплате выключены",
    )


async def _toggle_value_setting(callback, key, val_a, val_b, default, title_a, title_b):
    """Generic two-value toggle (e.g. list↔text, on↔off) with a friendly alert."""
    # REG-02 (06-07): final-coverage sweep — every key routed through this helper
    # (reg_university_mode/edu_conditional/reg_show_progress/payment_reminders_enabled) is
    # in SETTINGS_SCHEMA with a registry default byte-identical to the `default` param.
    current = await get_setting_typed(key)
    new_val = val_b if current == val_a else val_a
    await set_setting(key, new_val)
    await callback.answer(title_a if new_val == val_a else title_b, show_alert=True)
    from handlers.admin_sections import settings_return_screen  # ленивый шов (20-04)
    text, kb = await settings_return_screen(callback.from_user.id, callback_data=callback.data)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data == "toggle_uni_mode")
async def toggle_uni_mode(callback: types.CallbackQuery):
    await _toggle_value_setting(
        callback, "reg_university_mode", "list", "text", "text",
        "🏫 ВУЗ: выбор из списка", "🏫 ВУЗ: свободный ввод",
    )


@router.callback_query(F.data == "toggle_edu_conditional")
async def toggle_edu_conditional(callback: types.CallbackQuery):
    await _toggle_value_setting(
        callback, "edu_conditional", "on", "off", "on",
        "🎓 ВУЗ/курс спрашиваются только у студентов", "🎓 ВУЗ/курс спрашиваются у всех",
    )


@router.callback_query(F.data == "toggle_show_progress")
async def toggle_show_progress(callback: types.CallbackQuery):
    await _toggle_value_setting(
        callback, "reg_show_progress", "on", "off", "off",
        "🔢 Нумерация вопросов включена", "🔢 Нумерация вопросов выключена",
    )


async def _apply_event_type_preset(event_type: str):
    """D-05: event type presets module flags; each is still manually overridable after.
    conference → payment+consent ON; forum → both OFF; custom → no change."""
    if event_type == "conference":
        await set_setting("payment_enabled", "on")
        await set_setting("consent_enabled", "on")
    elif event_type == "forum":
        await set_setting("payment_enabled", "off")
        await set_setting("consent_enabled", "off")
    # "custom" → no change (manual control)


@router.callback_query(F.data == "settings_toggle_notify")
async def toggle_notify_mode(callback: types.CallbackQuery):
    # REG-02 (06-05): current-value read migrated to the registry; write path unchanged.
    current = await get_setting_typed("pending_notify_mode")
    new_val = "batched" if current == "instant" else "instant"
    await set_setting("pending_notify_mode", new_val)
    await callback.answer(f"Уведомление: {'📨 Сразу' if new_val == 'instant' else '🕒 Пачкой'}", show_alert=True)
    from handlers.admin_sections import settings_return_screen  # ленивый шов (20-04)
    text, kb = await settings_return_screen(callback.from_user.id, callback_data=callback.data)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data == "settings_toggle_bonus")
async def toggle_bonus(callback: types.CallbackQuery):
    # REG-02 (06-05): current-value read migrated to the registry; write path unchanged.
    current = await get_setting_typed("reg_bonus_enabled")
    new_val = "on" if current == "off" else "off"
    await set_setting("reg_bonus_enabled", new_val)

    label = "✅ Вкл" if new_val == "on" else "❌ Выкл"
    await callback.answer(f"Бонус за регистрацию: {label}", show_alert=True)

    from handlers.admin_sections import settings_return_screen  # ленивый шов (20-04)
    text, kb = await settings_return_screen(callback.from_user.id, callback_data=callback.data)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith("settings_file:"))
async def settings_file_start(callback: types.CallbackQuery, state: FSMContext):
    prefix = callback.data.split(":", 1)[1]
    prompts = {p: (label, prompt) for p, label, prompt in FILE_FIELDS}
    label, prompt = prompts.get(prefix, ("Файл", "Отправьте файл."))

    photo = await get_setting(f"{prefix}_photo_file_id")
    doc = await get_setting(f"{prefix}_doc_file_id")
    status = "✅ загружен" if (photo or doc) else "<i>не загружен</i>"
    text = f"{label}: {status}\n\n{prompt}"

    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="settings_cancel")],
    ])
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=cancel_kb)
    await state.set_state(EditSetting.waiting_for_file)
    await state.set_data({"file_setting": prefix})  # поток владеет данными один (см. consent_pdf_set)
    await callback.answer()


# ── Phase 09.2 (C, CITY-05): «🏙 Для города…» per-setting override sub-flow ────────────────
#
# One reusable screen for every per_city-flagged SETTINGS_SCHEMA key (text or enum) — reached
# from the editor (settings_edit_start below) and from the landing's «🏙 Форма по городам»
# shortcut for registration_mode, which has no settings_edit screen of its own. Mirrors the
# admin_city_switch/_roles_city_kb idiom (RESEARCH Pattern 3): city LABELS only, never codes.

async def _per_city_visible_codes(admin_id: int) -> list[str]:
    """Which city codes this admin may edit — a RIGHT, not a filter (Phase 07.2 terminology).
    Phase 09.3 (06, CITY-09): the only caller-facing question this answers now is "can this
    admin edit the city currently sitting in the header" (membership test), not "which cities
    should a picker list" — there is no picker left. Superadmins (config.ADMIN_IDS) see every
    city; a manager bound to a city (get_staff_city) sees exactly that one; an unbound manager
    sees all. This shapes the keyboard only — every write handler below (settings_edit_city /
    settings_reset_city_go) re-checks membership itself before writing anything (RESEARCH
    Pitfall 6: a hidden button is not access control)."""
    if admin_id in config.ADMIN_IDS:
        return city_codes()
    bound = await get_staff_city(admin_id)
    if bound:
        return [normalize_city(bound)]
    return city_codes()


async def _settings_edit_screen(key: str, header_code: str | None) -> tuple[str, InlineKeyboardMarkup]:
    """Phase 09.3 (06, CITY-09): single render helper for the per-key editor, relative to an
    ALREADY-RESOLVED header code (WR-05 — every caller resolves `admin_selected_city()` once
    and passes the result in here; this helper never calls it a second time). Reused by
    `settings_edit_start`, the `per_city_base` return path in `settings_edit_value`, and the
    reset confirm/go handlers below — one screen shape per branch, no separate picker screen.

    Three branches (CONTEXT B):
    (1) header is a real city AND `key` is per_city -> the city's OWN value or «как везде» +
        «✏️ Изменить для {город}» / «↩️ Как везде» (reset row only when an own value exists).
        No FSM is implied by this screen alone — the caller decides (branch (1) never starts
        one from here; only `settings_edit_city` does, on an explicit tap).
    (2) header is a real city AND `key` is NOT per_city -> today's prompt screen plus a
        global-only note (see the literal marker string in the branch below); the key is
        edited globally.
    (3) header is `None`/`ALL_CITIES` (module off or «все города») -> today's prompt screen,
        byte-identical to before the phase, minus the removed «🏙 Для города…» row."""
    prompts = {k: prompt for k, _, prompt in SETTINGS_FIELDS}
    # Phase 8 (ROLE-02): role_caps_<role> etc. ride this generic edit flow but aren't in
    # SETTINGS_FIELDS (D-18) — fall back to the registry itself for the prompt (Phase 6
    # D-13, registry-as-source) before the last-resort literal.
    prompt = prompts.get(key) or SETTINGS_SCHEMA.get(key, {}).get("prompt") or "Введите значение"
    per_city_ctx = bool(header_code and header_code != ALL_CITIES)
    # Quick 260822: списочный ключ правится по пунктам (handlers/admin_settings_lists.py) —
    # кнопки ➕/🗑/✏️ вместо ввода, FSM с этого экрана не стартует (см. settings_edit_start).
    entry = SETTINGS_SCHEMA.get(_base_setting_key(key), {})
    is_list, label = entry.get("type") == "list", entry.get("label", key)

    if per_city_ctx and is_per_city(key):
        city_label_txt = await city_label(header_code)
        composed = per_city_key(key, header_code)
        own_value = await get_setting(composed) if composed else None
        lines = [f"🏙 {html_module.escape(city_label_txt)}"]
        if own_value:
            lines.append(f"Своё значение: <b>{html_module.escape(own_value)}</b>")
        else:
            global_value = await get_setting(key)
            global_txt = f"<b>{html_module.escape(global_value)}</b>" if global_value else "<i>по умолчанию</i>"
            lines.append(f"Как везде. Общий текст: {global_txt}")

        rows: list[list[InlineKeyboardButton]] = [
            [InlineKeyboardButton(
                text=f"✏️ Изменить для {city_label_txt}",
                callback_data=f"settings_edit_city:{key}",
            )],
        ]
        if is_list:
            rows = admin_settings_lists.list_edit_rows(key)
        if own_value:
            rows.append([InlineKeyboardButton(text="↩️ Как везде", callback_data=f"settings_reset_city:{key}")])
        rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="settings_cancel")])
        return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)

    # Branches (2)/(3): today's prompt screen — escape both the field description (may
    # contain literal <b>/<code> examples) and the current value (admin may have stored raw
    # HTML) — otherwise parse_mode=HTML breaks.
    current = await get_setting(key)
    text = f"{html_module.escape(prompt)}"
    # Quick 260820-rms / 260822: списочные настройки (источники, города, варианты
    # мультивыбора) дважды схлопывались в один пункт — менеджер присылал один пункт, а
    # сообщение заменяло весь список. Теперь список показан по пунктам, а правка — кнопками:
    # «➕ Добавить пункт» / «🗑 Удалить пункт» / «✏️ Заменить список целиком».
    if is_list:
        items = admin_settings_lists.split_list_items(current)
        listed = "\n".join(f"• {html_module.escape(i)}" for i in items) or "<i>пусто</i>"
        text = (
            f"{html_module.escape(label)}\n\nСейчас в списке ({len(items)}):\n{listed}"
            "\n\n<i>Добавьте или уберите один пункт кнопками ниже. Переписать всё сразу — "
            "«✏️ Заменить список целиком».</i>"
        )
    elif current:
        text = f"Сейчас задано:\n<b>{html_module.escape(current)}</b>\n\n{text}"
    if not is_list:
        text += "\n\n<i>Пришлите новое значение сообщением. Чтобы очистить поле — отправьте «-».</i>"

    if per_city_ctx:
        # Branch (2): real city header, but the key is not per_city — правится глобально,
        # marked so the manager never mistakes this for a city-scoped edit.
        text = (
            f"🏙 {html_module.escape(await city_label(header_code))}\n"
            f"Общая настройка (одна на все города)\n\n{text}"
        )
    elif is_per_city(key) and await cities_module_on():
        # Branch (3), city module on (header None only when module off — see
        # admin_selected_city — or explicit «все города»): keep the override summary, drop
        # the removed «🏙 Для города…» entry point (CONTEXT B).
        override_codes = await city_override_codes(key)
        if override_codes:
            names = ", ".join([await city_label(c) for c in override_codes])
            text += f"\n\nПереопределено для: {names}"

    rows = admin_settings_lists.list_edit_rows(key) if is_list else []
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="settings_cancel")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@router.callback_query(F.data.startswith("settings_edit:"))
async def settings_edit_start(callback: types.CallbackQuery, state: FSMContext):
    key = callback.data.split(":", 1)[1]
    admin_id = callback.from_user.id
    # Phase 09.3 (06, CITY-09): WR-05 — single header read for this handler, passed into the
    # shared render helper so it never re-resolves the header itself.
    header_code = await admin_selected_city(admin_id)
    text, cancel_kb = await _settings_edit_screen(key, header_code)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=cancel_kb)

    # Branch (1) (header = real city AND key is per_city) never starts the FSM from here —
    # «✏️ Изменить для …» (settings_edit_city below) is the only entry point that may start
    # it, otherwise a stray text message sent while just LOOKING at the screen would silently
    # overwrite the global value. state.clear() is defensive: re-entering this screen (e.g.
    # via the «❌ Отмена» button on the per-city input screen) must never leave a stale FSM
    # state pointing at the wrong composite key.
    # Quick 260822: a list key never starts the FSM from here either — input begins only
    # from its own buttons (admin_settings_lists), so a stray message can't replace the list.
    own_city_context = bool(header_code and header_code != ALL_CITIES and is_per_city(key))
    is_list = SETTINGS_SCHEMA.get(key, {}).get("type") == "list"
    await state.clear()
    if not own_city_context and not is_list:
        await state.set_state(EditSetting.waiting_for_value)
        await state.update_data(setting_key=key)
    await callback.answer()


@router.callback_query(F.data.startswith("settings_edit_city:"))
async def settings_edit_city(callback: types.CallbackQuery, state: FSMContext):
    """Phase 09.3 (06, CITY-09): «✏️ Изменить для {город}» — the ONLY entry point that starts
    `EditSetting.waiting_for_value` for a per-city composite key; reuses 09.2-05's per-city
    text-entry mechanics verbatim (same FSM keys, same composed-key primitive), just reached
    from the header-aware editor screen instead of the deleted separate city picker."""
    key = callback.data.split(":", 1)[1]
    admin_id = callback.from_user.id
    # Fail-closed (RESEARCH Pattern 2): module off or a non-per_city key never starts an
    # edit, even if someone forges the callback_data directly.
    if not await cities_module_on() or not is_per_city(key):
        await callback.answer("Города выключены", show_alert=True)
        return
    header_code = await admin_selected_city(admin_id)
    if not header_code or header_code == ALL_CITIES:
        await callback.answer("Сначала выберите город в шапке", show_alert=True)
        return
    # T-093-19/21: RIGHT re-checked here, not just via a hidden button.
    visible = await _per_city_visible_codes(admin_id)
    if header_code not in visible:
        await callback.answer("Этот город правит суперадмин", show_alert=True)
        return
    # T-093-20: composed key comes ONLY from cities.per_city_key.
    composed = per_city_key(key, header_code)
    if composed is None:
        await callback.answer("Неизвестный город", show_alert=True)
        return

    entry = SETTINGS_SCHEMA.get(key, {})
    prompts = {k: prompt for k, _, prompt in SETTINGS_FIELDS}
    prompt = prompts.get(key) or entry.get("prompt") or "Введите значение"
    current = await get_setting(composed)
    city_txt = await city_label(header_code)
    text = f"🏙 {html_module.escape(city_txt)}\n\n"
    if current:
        text += f"Сейчас у города:\n<b>{html_module.escape(current)}</b>\n\n"
    else:
        text += "Сейчас у города: <i>как везде</i>\n\n"
    text += html_module.escape(prompt)
    text += "\n\n<i>Пришлите новое значение сообщением. Чтобы очистить поле — отправьте «-».</i>"

    rows: list[list[InlineKeyboardButton]] = []
    if current:
        rows.append([InlineKeyboardButton(text="↩️ Как везде", callback_data=f"settings_reset_city:{key}")])
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data=f"settings_edit:{key}")])
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await state.set_state(EditSetting.waiting_for_value)
    await state.set_data({"setting_key": composed, "per_city_base": key})  # поток владеет данными один
    await callback.answer()


@router.callback_query(F.data.startswith("settings_reset_city:"))
async def settings_reset_city(callback: types.CallbackQuery):
    """Confirm screen for «↩️ Как везде» on the header-scoped per-key editor — same two-step
    confirm gate idiom as `settings_regmode_reset` above (09.2-05 lineage: names the value
    the city is about to fall back to before deleting anything)."""
    key = callback.data.split(":", 1)[1]
    admin_id = callback.from_user.id
    if not await cities_module_on() or not is_per_city(key):
        await callback.answer("Города выключены", show_alert=True)
        return
    header_code = await admin_selected_city(admin_id)
    if not header_code or header_code == ALL_CITIES:
        await callback.answer("Нет своего значения для сброса", show_alert=True)
        return
    visible = await _per_city_visible_codes(admin_id)
    if header_code not in visible:
        await callback.answer("Этот город правит суперадмин", show_alert=True)
        return
    composed = per_city_key(key, header_code)
    if composed is None or not await get_setting(composed):
        await callback.answer("Нет своего значения для сброса", show_alert=True)
        return

    city_txt = html_module.escape(await city_label(header_code))
    global_value = await get_setting(key)
    preview = f"<b>{html_module.escape(global_value)}</b>" if global_value else "<i>по умолчанию</i>"
    text = (
        f"Город {city_txt} снова будет использовать общий текст:\n{preview}\n\n"
        "Свой текст города будет удалён."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, как везде", callback_data=f"settings_reset_city_go:{key}:{header_code}")],
        [InlineKeyboardButton(text="← Отмена", callback_data=f"settings_edit:{key}")],
    ])
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("settings_reset_city_go:"))
async def settings_reset_city_go(callback: types.CallbackQuery):
    rest = callback.data.split(":", 1)[1]
    if ":" not in rest:
        await callback.answer("Неизвестная кнопка", show_alert=True)
        return
    key, code = rest.rsplit(":", 1)
    admin_id = callback.from_user.id
    if not await cities_module_on() or not is_per_city(key):
        await callback.answer("Города выключены", show_alert=True)
        return
    # T-093-20: composed key comes ONLY from cities.per_city_key — refuse on an unknown code
    # before touching rights or freshness (same guard order as settings_regmode_reset_go above).
    composed = per_city_key(key, code)
    if composed is None:
        await callback.answer("Неизвестный город", show_alert=True)
        return
    # T-093-19: RIGHT check against the code carried in callback_data (not just the current
    # header) — this is what catches a bound manager's forged confirmation for another city,
    # since the freshness check below alone could never distinguish "forged" from "stale"
    # (a bound manager's OWN header can never actually become another city).
    visible = await _per_city_visible_codes(admin_id)
    if code not in visible:
        await callback.answer("Этот город правит суперадмин", show_alert=True)
        return
    # T-093-22: freshness — the confirm screen named the header's city; if the header moved
    # on since, refuse and re-render the editor for the NEW header instead of deleting.
    current = await admin_selected_city(admin_id)
    if code != current:
        await callback.answer("Город админки изменился — подтвердите заново.", show_alert=True)
        text, kb = await _settings_edit_screen(key, current)
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        return

    await delete_setting(composed)  # idempotent — safe if already absent
    city_txt = await city_label(code)
    await callback.answer(f"Готово: {city_txt} — как везде", show_alert=True)
    text, kb = await _settings_edit_screen(key, current)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith("settings_photo:"))
async def settings_photo_start(callback: types.CallbackQuery, state: FSMContext):
    prefix = callback.data.split(":", 1)[1]
    prompts = {p: (label, prompt) for p, label, prompt in PHOTO_FIELDS}
    label, prompt = prompts.get(prefix, ("Фото", "Отправьте фото."))

    current = await get_setting(f"{prefix}_photo_file_id")
    text = f"{label}: {'✅ загружена' if current else '<i>не загружена</i>'}\n\n{prompt}"

    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="settings_cancel")],
    ])
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=cancel_kb)
    await state.set_state(EditSetting.waiting_for_photo)
    await state.set_data({"photo_setting": prefix})  # поток владеет данными один (см. consent_pdf_set)
    await callback.answer()


def _return_hint_from_state(data: dict, raw_state: str | None = None) -> dict:
    """Phase 20 (20-04): подсказка «откуда пришли» для `settings_return_screen`, собранная из
    данных FSM. Объявлена ОДИН раз и используется обеими отменами (кнопкой «❌ Отмена» и
    командой «/cancel»): дважды написанная развилка разъехалась бы при первом же новом ключе.

    Развилка идёт ПО СОСТОЯНИЮ: оно однозначно называет поток, а ключ в данных — только его
    параметр. Порядок «первый найденный ключ» (запасной проход ниже) разъезжается с реальностью
    в тот день, когда кто-нибудь вернёт домешивание данных на входе в поток: отмена загрузки
    фото уведёт менеджера на экран группы брошенной ТЕКСТОВОЙ правки.

    T-20-20: пустые/протухшие данные (MemoryStorage сбрасывается при рестарте) дают `{}` —
    резолвер уйдёт на корень, необработанного исключения и «зависшего» сообщения не будет."""
    # `consent_pdf_{key}` — не ключ SETTINGS_SCHEMA, группы у него нет: резолвер вернёт
    # менеджера в РАЗДЕЛ-владелец экрана «🧾 PDF согласий», то есть в «📝 Анкета».
    flows = (
        (EditSetting.waiting_for_value, "setting_key", lambda v: {"setting_key": v}),
        (EditSetting.waiting_for_photo, "photo_setting", lambda v: {"setting_key": f"{v}_photo_file_id"}),
        (EditSetting.waiting_for_file, "raw_file_key", lambda _v: {"callback_data": "admin_consent_pdfs"}),
        (EditSetting.waiting_for_file, "file_setting", lambda v: {"setting_key": f"{v}_doc_file_id"}),
    )
    for flow, key, hint in flows:
        if raw_state == flow.state and data.get(key):
            return hint(data[key])
    for _flow, key, hint in flows:  # состояние не названо (старый вызов) — порядок по данным
        if data.get(key):
            return hint(data[key])
    return {}


@router.callback_query(F.data == "settings_cancel")
async def cancel_edit_setting_callback(callback: types.CallbackQuery, state: FSMContext):
    from handlers.admin_sections import settings_return_screen  # ленивый шов (20-04)
    # Данные и состояние читаются ДО очистки — после неё подсказку взять уже неоткуда.
    hint = _return_hint_from_state(await state.get_data(), await state.get_state())
    await state.clear()
    text, kb = await settings_return_screen(callback.from_user.id, **hint)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data == "admin_sync_sheet")
async def sync_sheet(callback: types.CallbackQuery):
    """UAT 25.08 (prod snapshot): sync used to always dozapisyvat missing delegates into the
    MAIN tab, even for delegates whose city routes them to a named tab — main tab («МСК») had
    46 rows that actually belonged to СПб/Тюмень. Routes each user through the SAME resolver the
    live append and rebuild_sheet use (city_row_tab), then appends missing rows per-tab, one
    try/except per city tab so a single tab failure never cancels the rest."""
    await callback.answer("🔄 Синхронизация...")
    await callback.message.edit_text("🔄 Получаю данные из таблицы...", parse_mode="HTML")

    try:
        headers = await active_sheet_headers()  # only enabled columns
        all_users = await get_all_users_dicts()

        main_users: list[dict] = []
        city_users: dict[str, list[dict]] = {}
        for u in all_users:
            tab = await city_row_tab(u.get("event_city"), u.get("participant_type"))
            if tab is None:
                main_users.append(u)
            else:
                city_users.setdefault(tab, []).append(u)

        # Основная вкладка — прежний порядок шагов, только теперь на своём подмножестве
        # пользователей (module off / нет городов -> main_users == all_users, поведение прежнее).
        await ensure_sheet_header(headers)  # шапка таблицы, если её ещё нет
        existing_ids = await get_existing_sheet_ids()
        main_missing = [u for u in main_users if u["telegram_id"] not in existing_ids]
        main_count = 0
        if main_missing:
            main_rows = [[_sheet_value_map(u).get(h, "-") for h in headers] for u in main_missing]
            main_count = await append_rows_to_sheet(main_rows)

        # Городские вкладки — каждая в своём try/except: одна упавшая не отменяет остальные.
        city_synced: list[tuple[str, int]] = []
        failed_tabs: list[str] = []
        for tab, trows in city_users.items():
            try:
                # Шапка ДО чтения id: чтение отбрасывает первую строку как шапку, и на вкладке
                # без шапки первый делегат выпал бы из набора существующих и продублировался.
                await ensure_named_sheet_header(tab, headers)
                ids = await get_existing_named_sheet_ids(tab)
                if ids is None:
                    failed_tabs.append(tab)
                    continue
                missing_named = [u for u in trows if u["telegram_id"] not in ids]
                if not missing_named:
                    city_synced.append((tab, 0))
                    continue
                rows = [[_sheet_value_map(u).get(h, "-") for h in headers] for u in missing_named]
                n = await append_rows_to_named_sheet(tab, rows)
                if n < 0:
                    failed_tabs.append(tab)
                else:
                    city_synced.append((tab, n))
            except Exception as e:
                logger.warning(f"sync_sheet: city tab {tab!r} failed: {e}")
                failed_tabs.append(tab)

        total_count = main_count + sum(n for _tab, n in city_synced)
        # WR-06: только счётчики по вкладкам и id админа — никаких строк данных в логе.
        logger.info(
            f"admin={callback.from_user.id} action=sync_sheet main_added={main_count} "
            f"city_tabs={len(city_users)} city_added={total_count - main_count} "
            f"failed_tabs={len(failed_tabs)}"
        )

        if not city_users:
            # Модуль городов выключен (или у всех делегатов основная вкладка) — байт-в-байт
            # прежнее поведение и прежний текст.
            if not main_missing:
                await callback.message.edit_text(
                    "✅ Таблица синхронизирована, пропущенных записей нет.",
                    parse_mode="HTML",
                    reply_markup=await admin_keyboard_for(callback.from_user.id),
                )
                return
            await callback.message.edit_text(
                f"✅ Синхронизация завершена!\n\n"
                f"Добавлено записей: <b>{main_count}</b>",
                parse_mode="HTML",
                reply_markup=await admin_keyboard_for(callback.from_user.id),
            )
            return

        lines = [
            "✅ Синхронизация завершена!",
            "",
            f"Добавлено записей: <b>{total_count}</b>",
            f"Основная вкладка: <b>{main_count}</b>",
        ]
        for tab, n in city_synced:
            lines.append(f"{html_module.escape(tab)}: <b>{n}</b>")
        for tab in failed_tabs:
            lines.append(f"{html_module.escape(tab)}: <b>❌</b>")
        if failed_tabs:
            names = ", ".join(html_module.escape(t) for t in failed_tabs)
            lines.append(
                f"⚠️ Не удалось обновить вкладки: {names}. Остальное записано, "
                f"попробуйте нажать «Синхронизация» ещё раз."
            )

        await callback.message.edit_text(
            "\n".join(lines),
            parse_mode="HTML",
            reply_markup=await admin_keyboard_for(callback.from_user.id),
        )
    except Exception as e:
        logger.error(f"Sheet sync failed: {e}")
        await callback.message.edit_text(
            f"❌ Ошибка синхронизации:\n<code>{html_module.escape(str(e))}</code>",
            parse_mode="HTML",
            reply_markup=await admin_keyboard_for(callback.from_user.id),
        )


@router.callback_query(F.data == "admin_rebuild_sheet")
async def rebuild_sheet_confirm(callback: types.CallbackQuery):
    """Quick 260813-sdl: пересборка делает sheet.clear() и перезаписывает ВСЕ строки — то есть
    сносит любые ручные правки менеджеров на листе. До этого она запускалась одним тапом, без
    вопроса; соседняя destructive-кнопка «🧹 Убрать дубли» подтверждение имела всегда. Гейт
    зеркалит dedupe: сама работа переехала в admin_rebuild_sheet_go."""
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="♻️ Да, пересобрать", callback_data="admin_rebuild_sheet_go")],
        [InlineKeyboardButton(text="← Отмена", callback_data="admin_menu")],
    ])
    await callback.message.edit_text(
        "♻️ <b>Пересобрать таблицу?</b>\n\n"
        "Перезапишу на основной вкладке <b>шапку и все строки</b> из базы бота: колонки "
        "встанут в порядке анкеты, «Статус» получит выпадашку и цвета.\n\n"
        "⚠️ Лист очищается целиком и заполняется заново. <b>Любые ручные правки и заметки, "
        "которых нет в базе бота, пропадут безвозвратно.</b> Если менеджеры что-то дописывали "
        "прямо в таблице — сначала сохраните копию (Файл → Создать копию).",
        parse_mode="HTML",
        reply_markup=kb,
    )
    await callback.answer()


@router.callback_query(F.data == "admin_rebuild_sheet_go")
async def rebuild_sheet(callback: types.CallbackQuery):
    """Полная пересборка листа данных: перезаписать шапку + ВСЕ строки в текущем порядке
    колонок, применить выпадашку/цвета к «Статус». Выравнивает старые строки после смены
    порядка колонок (Таня п.1/п.5). Внимание: перезаписывает ручные правки на листе."""
    await callback.answer("♻️ Пересборка...")
    logger.info(f"admin={callback.from_user.id} action=rebuild_sheet start")
    await callback.message.edit_text("♻️ Пересобираю таблицу (перезапись всех строк)…", parse_mode="HTML")

    try:
        headers = await active_sheet_headers()  # only enabled columns
        all_users = await get_all_users_dicts()
        # UAT 17.08 (fast): the rebuild used to dump EVERY user into the main tab regardless of
        # city, so after one «Пересобрать» the main tab held СПб/Тюмень rows too while live
        # appends kept routing them to their own tabs -- the sheets drifted apart. Route each
        # row through the SAME resolver the live append uses (city_row_tab: default city / module
        # off -> None -> main tab; other city -> its named tab) and full-refresh every touched
        # city tab alongside the main one. Module off => city_row_tab is always None => byte-
        # identical to the old behaviour.
        main_rows: list[list] = []
        city_rows: dict[str, list[list]] = {}
        for u in all_users:
            row = [_sheet_value_map(u).get(h, "-") for h in headers]
            tab = await city_row_tab(u.get("event_city"), u.get("participant_type"))
            if tab is None:
                main_rows.append(row)
            else:
                city_rows.setdefault(tab, []).append(row)
        rows = main_rows
        count = await rebuild_main_sheet(headers, rows)
        city_synced: list[tuple[str, int]] = []
        if count >= 0:
            for tab, trows in city_rows.items():
                city_synced.append((tab, await sync_named_worksheet(tab, headers, trows)))
        if count == REFUSED_UNPINNED_TAB:
            await callback.message.edit_text(
                "⛔ Пересборка отключена: основная вкладка не задана.\n\n"
                "Без неё пересборка могла бы задеть не ту вкладку. Укажите вкладку в "
                "«⚙️ Настройки → 📄 Вкладки таблицы → 📄 Основная (регистрации)» — сработает "
                "сразу, без перезапуска. Вариант для разработчика — <code>GOOGLE_SHEET_TAB</code> "
                "в .env (тогда нужен перезапуск).",
                parse_mode="HTML",
                reply_markup=await admin_keyboard_for(callback.from_user.id),
            )
            return
        if count < 0:
            await callback.message.edit_text(
                "❌ Пересборка не выполнена (таблица не настроена или ошибка API). Смотри логи.",
                parse_mode="HTML",
                reply_markup=await admin_keyboard_for(callback.from_user.id),
            )
            return
        # CR-9: rebuild is the re-sync point — freeze the snapshot to the header just written
        # so subsequent registrations align to the rebuilt physical header.
        await set_sheet_schema(headers)
        city_line = ""
        if city_synced:
            parts = [f"{html_module.escape(t)}: <b>{n if n >= 0 else '❌'}</b>" for t, n in city_synced]
            city_line = "Городские вкладки: " + ", ".join(parts) + "\n"
        await callback.message.edit_text(
            f"✅ Таблица пересобрана!\n\n"
            f"Строк записано (основная): <b>{count}</b>\n"
            f"{city_line}"
            f"Колонки выстроены в порядке анкеты, «Статус» с выпадашкой и цветами.",
            parse_mode="HTML",
            reply_markup=await admin_keyboard_for(callback.from_user.id),
        )
    except Exception as e:
        logger.error(f"Sheet rebuild failed: {e}")
        await callback.message.edit_text(
            f"❌ Ошибка пересборки:\n<code>{html_module.escape(str(e))}</code>",
            parse_mode="HTML",
            reply_markup=await admin_keyboard_for(callback.from_user.id),
        )


@router.callback_query(F.data == "settings_back")
async def settings_back_to_admin(callback: types.CallbackQuery):
    await callback.message.edit_text(
        "👮‍♂️ <b>Панель администратора</b>",
        parse_mode="HTML",
        reply_markup=await admin_keyboard_for(callback.from_user.id),
    )
    await callback.answer()


def _parse_consent_list(raw: str) -> list[tuple[str, str]]:
    """consent_list ('Название|ключ' per line) → [(label, key)]."""
    items = []
    # ';' works as a separator too — see _consent_entries in registration.py: the
    # Telegram Enter=send trap can split multi-line input, so admins may enter all
    # consents on one line joined by ';'. Existing newline data still parses.
    for line in (raw or "").replace(";", "\n").strip().splitlines():
        line = line.strip()
        if not line or "|" not in line:
            continue
        label, key = line.split("|", 1)
        key = key.strip()
        if key:
            items.append((label.strip() or key, key))
    return items


@router.callback_query(F.data == "admin_consent_pdfs")
async def admin_consent_pdfs(callback: types.CallbackQuery):
    # Phase 20 (20-04): «Назад» ведёт в раздел-владелец этого экрана — «📝 Анкета».
    from handlers.admin_sections import back_button  # ленивый шов
    items = _parse_consent_list(await get_setting("consent_list") or "")
    if not items:
        await callback.answer()
        await callback.message.edit_text(
            "🧾 <b>PDF согласий</b>\n\n"
            "Здесь пусто, потому что ещё не задан список согласий.\n\n"
            "<b>Что сделать:</b>\n"
            "1. Зайди в «📋 Список согласий» и добавь согласия (каждое строкой "
            "<i>Название|ключ</i>).\n"
            "2. Вернись сюда — у каждого согласия появится кнопка для загрузки PDF.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📋 К списку согласий", callback_data="settings_edit:consent_list")],
                [back_button("admin_consent_pdfs")],
            ]),
        )
        return
    buttons = []
    for label, key in items:
        has_pdf = bool(await get_setting(f"consent_pdf_{key}"))
        mark = "✅" if has_pdf else "📎"
        buttons.append([InlineKeyboardButton(text=f"{mark} {label}", callback_data=f"consent_pdf_set:{key}")])
    buttons.append([back_button("admin_consent_pdfs")])
    await callback.message.edit_text(
        "🧾 <b>PDF согласий</b>\n\n"
        "Нажми на согласие и пришли PDF-файл — участник увидит его прикреплённым к этому согласию.\n\n"
        "✅ — PDF уже загружен · 📎 — ещё нет.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("consent_pdf_set:"))
async def consent_pdf_set(callback: types.CallbackQuery, state: FSMContext):
    key = callback.data.split(":", 1)[1]
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="settings_cancel")],
    ])
    await callback.message.edit_text(
        "📎 Пришли сюда <b>PDF-файл</b> этого согласия одним сообщением "
        "(перетащи файл или прикрепи через скрепку).\n\n"
        "Просто фото или ссылка не подойдут — нужен именно PDF-документ.",
        parse_mode="HTML",
        reply_markup=cancel_kb,
    )
    await state.set_state(EditSetting.waiting_for_file)
    # Данные задаются ЦЕЛИКОМ (set_data): иначе брошенный тут промпт переживал уход менеджера
    # на «📎 Бонус за регистрацию» — приёмник документа разбирает `raw_file_key` раньше
    # `file_setting`, и PDF бонуса молча уезжал в согласие. Правило на каждом входе в поток.
    await state.set_data({"raw_file_key": f"consent_pdf_{key}"})
    await callback.answer()


@router.message(StateFilter(EditSetting), Command("cancel"))
@router.message(StateFilter(EditSetting), F.text == "Отмена")
async def cancel_edit_setting(message: types.Message, state: FSMContext):
    from handlers.admin_sections import settings_return_screen  # ленивый шов (20-04)
    hint = _return_hint_from_state(await state.get_data(), await state.get_state())  # ДО очистки
    await state.clear()
    text, kb = await settings_return_screen(message.from_user.id, **hint)
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.message(EditSetting.waiting_for_photo, F.photo)
async def settings_receive_photo(message: types.Message, state: FSMContext):
    data = await state.get_data()
    prefix = data.get("photo_setting", "program")

    file_id = message.photo[-1].file_id
    await set_setting(f"{prefix}_photo_file_id", file_id)

    if message.caption:
        await set_setting(f"{prefix}_caption", message.html_text)
    else:
        await delete_setting(f"{prefix}_caption")

    await state.clear()
    await message.answer("✅ Фото обновлено!")
    # Phase 20 (20-04): возврат на экран группы, где живёт эта кнопка «📷 …» — подсказка —
    # ключ, который только что записан. Подтверждение остаётся отдельным сообщением выше.
    from handlers.admin_sections import settings_return_screen  # ленивый шов
    text, kb = await settings_return_screen(message.from_user.id, setting_key=f"{prefix}_photo_file_id")
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.message(EditSetting.waiting_for_photo)
async def settings_receive_photo_invalid(message: types.Message):
    await message.answer("Отправьте именно фото (не файлом).")


@router.message(EditSetting.waiting_for_file, F.photo)
async def settings_receive_file_photo(message: types.Message, state: FSMContext):
    data = await state.get_data()
    if data.get("raw_file_key"):
        await message.answer("Согласие принимается только PDF-документом, не фото.")
        return
    prefix = data.get("file_setting", "reg_bonus")

    file_id = message.photo[-1].file_id
    await set_setting(f"{prefix}_photo_file_id", file_id)
    await delete_setting(f"{prefix}_doc_file_id")

    if message.caption:
        await set_setting(f"{prefix}_caption", message.html_text)
    else:
        await delete_setting(f"{prefix}_caption")

    await state.clear()
    await message.answer("✅ Файл обновлён!")
    # Фото, присланное файлом, пишется в тот же «{prefix}_photo_file_id» — и экран возврата
    # тот же, что у обычного фото (20-04).
    from handlers.admin_sections import settings_return_screen  # ленивый шов
    text, kb = await settings_return_screen(message.from_user.id, setting_key=f"{prefix}_photo_file_id")
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.message(EditSetting.waiting_for_file, F.document)
async def settings_receive_file_doc(message: types.Message, state: FSMContext):
    from handlers.admin_sections import settings_return_screen  # ленивый шов (20-04)
    data = await state.get_data()

    # Consent PDF: store the document file_id directly into an arbitrary settings key.
    raw_key = data.get("raw_file_key")
    if raw_key:
        if (message.document.mime_type or "") != "application/pdf":
            await message.answer("Принимается только PDF-документ. Пришли PDF.")
            return
        await set_setting(raw_key, message.document.file_id)
        await state.clear()
        await message.answer("✅ PDF согласия сохранён!")
        # Phase 20 (20-04): `raw_file_key` вида «consent_pdf_{key}» — не ключ SETTINGS_SCHEMA,
        # группы у него нет, а экран есть: возвращаем на «🧾 PDF согласий».
        text, kb = await settings_return_screen(message.from_user.id, callback_data="admin_consent_pdfs")
        await message.answer(text, parse_mode="HTML", reply_markup=kb)
        return

    prefix = data.get("file_setting", "reg_bonus")

    file_id = message.document.file_id
    await set_setting(f"{prefix}_doc_file_id", file_id)
    await delete_setting(f"{prefix}_photo_file_id")

    if message.caption:
        await set_setting(f"{prefix}_caption", message.html_text)
    else:
        await delete_setting(f"{prefix}_caption")

    await state.clear()
    await message.answer("✅ Файл обновлён!")
    text, kb = await settings_return_screen(message.from_user.id, setting_key=f"{prefix}_doc_file_id")
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.message(EditSetting.waiting_for_file)
async def settings_receive_file_invalid(message: types.Message):
    await message.answer("Отправьте фото или документ.")


HTML_SETTINGS = {
    "start_text", "start_text_registered", "start_text_returning", "reg_complete_text",
    "approve_text", "approve_text__party",
    # Phase 17.1 (17.1-03): единая политика для текстовых ключей 17.1 — если prompt обещает
    # менеджеру «Поддерживается HTML», ввод берётся из message.html_text (жирный/курсив из
    # Telegram сохраняются, «<»/«&» экранируются сами), как у соседей выше. Ключи, которые
    # консьюмер дополнительно html.escape'ит (preselect_*) или шлёт с parse_mode=None,
    # сюда НЕ входят. Инвариант «prompt говорит HTML <=> ключ здесь» сторожит
    # tests/test_delegate_texts_registry_260819.py::test_html_promise_in_prompt_matches_html_settings.
    "pending_gate_text",
    "poll_intro_text",  # опросы: вступление шлётся send_message с parse_mode=HTML (default бота)
    "start_returning_cta_text", "recall_generic_prompt_text",
    "payment_option_picker_header_text", "payment_details_template_text",
    "payment_pay_later_text", "payment_pay_later_menu_hint_text",
    "payment_receipt_received_text",
    "leaderboard_header_text", "leaderboard_rank_line_text",
    "balance_history_header_text", "referral_list_header_text",
    "game_wizard_preview_title",  # Phase 16 (16-03): заголовок превью финального шага визарда
    "program_empty_text", "speakers_empty_text", "contacts_empty_text",
    "ask_question_prompt_text", "ask_question_sent_text",
}


def _base_setting_key(key: str) -> str:
    """Phase 09.2 (C, CITY-05): strips a `{key}__city__{code}` composite key down to the
    base registry key — used ONLY for the HTML_SETTINGS membership check in
    settings_edit_value, so per-city text saves get the same HTML parsing as the global
    save. `_SHEET_TAB_WRITE_MODE`/`_options` branches deliberately stay on the raw key
    (guarded by test_no_per_city_key_in_sheet_tab_write_mode_or_options_suffix)."""
    return key.split(PER_CITY_SEP)[0]


def _enum_human_label(key: str, value: str) -> str:
    """Human-readable alert text for a per-city enum toggle (CLAUDE.md: no raw values in
    admin-facing alerts)."""
    if key == "registration_mode":
        return {"short": "⚡ Краткая", "full": "📋 Полная"}.get(value, value)
    if value == "on":
        return "✅ Вкл"
    if value == "off":
        return "❌ Выкл"
    return value


# Quick 260815-3hw (Task 3): which Google Sheets tab-name keys the bot actually WRITES to, and
# HOW. "rewrite" = the sync path does ws.clear() + full rewrite (rebuild_main_sheet /
# sync_named_worksheet); "append" = only new rows are ever added (append_to_named_sheet), never
# a clear. preselect_tab (read-only — the bot never writes it) and the five
# city_tab_suffix__* keys (not full tab names, just suffixes — incl. the per-city gamification
# suffixes __game/__game_history) are deliberately ABSENT — the confirm-gate in
# settings_edit_value only fires for a key present in this dict.
_SHEET_TAB_WRITE_MODE = {
    "main_sheet_tab": "rewrite",
    "incomplete_sheet_tab": "rewrite",
    "game_matrix_tab": "rewrite",
    "game_history_tab": "rewrite",
    "short_sheet_tab": "append",
    "party_sheet_tab": "append",
}


async def _after_tab_setting_saved(key: str) -> None:
    """Called after EVERY save/clear of a _SHEET_TAB_WRITE_MODE key (plain save, gated
    save-after-confirm, and the "-" clear path) — resets the cached MAIN worksheet handle
    (services.sheets._sheet global) so a renamed main_sheet_tab takes effect on the very next
    write, no bot restart needed. Named-tab caches (short/party/game/incomplete) need no
    reset: they're keyed BY NAME (services.sheets._named_sheets), so a new name simply opens a
    new cache entry — the stale entry under the old name just goes unused, it isn't wrong."""
    if key == "main_sheet_tab":
        _reset_sheet_cache()


def _tab_confirm_text(key: str, value: str, rows: int) -> str:
    """Confirm-screen body for an EXISTING tab name — text differs by write mode (CLAUDE.md:
    a confirmation has to name the actual damage, and for an append-only tab nothing is
    actually lost)."""
    label = SETTINGS_SCHEMA.get(key, {}).get("label", key)
    safe_value = html_module.escape(value)
    mode = _SHEET_TAB_WRITE_MODE.get(key)
    if mode == "append":
        body = (
            f"Вкладка «{safe_value}» уже существует, в ней {rows} строк.\n\n"
            "Бот будет дописывать в неё строки заявок, к тому, что там уже есть — ничего не "
            "сотрётся."
        )
    else:
        body = (
            f"Вкладка «{safe_value}» уже существует, в ней {rows} строк.\n\n"
            "Бот будет перезаписывать её целиком при каждой синхронизации — <b>всё, что там "
            "сейчас есть, пропадёт.</b>"
        )
        if key == "main_sheet_tab":
            body += (
                "\n\nРегистрации будут дописываться в неё по одной; кнопка «♻️ Пересобрать "
                "таблицу» очистит её целиком и запишет заново."
            )
    return f"⚠️ <b>{html_module.escape(label)}</b>\n\n{body}"


def _tab_check_failed_warning(key: str) -> str:
    """Appended to the post-save confirmation text when tab_row_count() couldn't check the
    spreadsheet at all (Sheets down/unconfigured) — the value is saved regardless (a settings
    change must never depend on Sheets being reachable), but the manager needs to know the
    existing-tab check didn't run."""
    mode = _SHEET_TAB_WRITE_MODE.get(key)
    if mode == "append":
        tail = "если такая вкладка уже есть, бот будет дописывать в неё, ничего не потеряется."
    else:
        tail = "если такая вкладка уже есть, при следующей синхронизации она будет перезаписана."
    return f"\n\n⚠️ Значение сохранено, но проверить вкладку в Google-таблице не удалось — {tail}"


def _tab_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, сохранить это имя", callback_data="sheets_tab_confirm")],
        [InlineKeyboardButton(text="← Отмена", callback_data="sheets_tab_cancel")],
    ])


@router.message(EditSetting.waiting_for_value)
async def settings_edit_value(message: types.Message, state: FSMContext):
    data = await state.get_data()
    key = data["setting_key"]

    # Phase 09.2 (C, CITY-05): a per-city composite key (`{base}__city__{code}`) gets the
    # SAME HTML-parsing treatment as its base key — the check is against the base, not the
    # raw composite (which is never itself a HTML_SETTINGS member).
    if _base_setting_key(key) in HTML_SETTINGS:
        value = (message.html_text or message.text or "").strip()
    else:
        value = (message.text or "").strip()

    # Guard: a non-text message (sticker/photo/voice/forwarded media) or a whitespace-only
    # send yields value == "" here. Storing "" is never a meaningful value — the registry's
    # text branch would return "" instead of the default (so the settings screen shows
    # «по умолчанию» while a consumer actually resolves ""), and an empty Google Sheets tab
    # name breaks the allowlist read and every sync. Clearing a setting is the explicit "-"
    # sentinel, not an empty send. Reject and stay in the state so the admin can just retype.
    if not value:
        await message.answer(
            "Не понял значение — пришлите его <b>текстом</b> одним сообщением "
            "(например: <code>Реги бот</code>).\n\nЧтобы очистить настройку, отправьте «-».",
            parse_mode="HTML",
        )
        return

    # Quick 260820-rms: команда — не значение. Админский роутер подключён ПЕРВЫМ (main.py),
    # поэтому /start, отправленный внутри правки настройки, до cmd_start не доходит и молча
    # ложится в bot_settings. 20.08 так были затёрты source_options и approve_text: делегаты
    # вместо списка источников получили одну кнопку «/start». Остаёмся в состоянии — менеджеру
    # достаточно набрать значение ещё раз.
    if is_command_like(value):
        await message.answer(
            f"<code>{html_module.escape(value)}</code> — это команда, а не значение настройки, "
            "сохранять её не стал.\n\nПришлите значение текстом, "
            "«-» — сбросить настройку, «❌ Отмена» — выйти без изменений.",
            parse_mode="HTML",
        )
        return

    # Review 09.3 WR-01 (TOCTOU): for a per-city composite key the RIGHT to write is
    # re-checked HERE, at write time — not only in settings_edit_city at FSM-entry time. The
    # composed key sat in FSM data while the admin typed; meanwhile their city binding may
    # have changed or they may have moved the header. Same guard order as
    # settings_reset_city_go: known code -> module on -> right -> freshness; on any failure
    # nothing is written and the FSM is cleared (fail-closed). Non-per-city keys are untouched.
    if PER_CITY_SEP in key:
        parsed = split_per_city_key(key)
        admin_id = message.from_user.id
        if parsed is None or not await cities_module_on():
            await state.clear()
            await message.answer("Города выключены — правка отменена.")
            return
        _base, code = parsed
        if code not in await _per_city_visible_codes(admin_id):
            await state.clear()
            await message.answer("Этот город правит суперадмин — правка отменена.")
            return
        if code != await admin_selected_city(admin_id):
            await state.clear()
            await message.answer("Город админки изменился — начните правку заново.")
            return

    # Quick 260819: type-aware validation (int / enum) BEFORE any write — see
    # handlers/settings_validation.py. On failure nothing is written and the FSM stays in
    # waiting_for_value so the admin just retypes. "-" (reset) bypasses validation.
    if value != "-":
        value, error = validate_setting_value(key, value)
        if error:
            await message.answer(error, parse_mode="HTML")
            return

    # Quick 260815-3hw (Task 3): confirm-gate before silently overwriting an EXISTING Google
    # Sheets tab — only for keys the bot actually writes to (_SHEET_TAB_WRITE_MODE);
    # preselect_tab (read-only) and the city_tab_suffix__* keys never reach this branch, and
    # neither does clearing a value ("-") — there is nothing to protect when unsetting.
    tab_check_failed = False
    if key in _SHEET_TAB_WRITE_MODE and value and value != "-":
        probe = await tab_row_count(value)
        if probe is None:
            tab_check_failed = True
        elif probe[0]:
            _exists, rows = probe
            await state.update_data(pending_tab_key=key, pending_tab_value=value)
            await state.set_state(EditSetting.waiting_for_tab_confirm)
            await message.answer(
                _tab_confirm_text(key, value, rows),
                parse_mode="HTML",
                reply_markup=_tab_confirm_keyboard(),
            )
            return
        # probe == (False, 0): tab doesn't exist yet — fall through to the normal silent save.

    # Quick 260820-rms: вторая половина аудита правок — db.set_setting пишет ЧТО изменилось,
    # эта строка пишет КТО. Без неё разбор «кто положил /start в источники» упирается в пустой
    # лог (20.08 именно так и вышло).
    logger.info(f"admin {message.from_user.id} правит настройку {key}")

    warning = ""
    if value == "-":
        await delete_setting(key)
    else:
        await set_setting(key, value)
        # Phase 4 (D-05): saving event_type applies the module-toggle preset.
        if key == "event_type":
            await _apply_event_type_preset(value.strip().lower())
            await remind_consent_purposes_after_preset(message, value.strip())
        # WR-02: "Отмена"/"Другое"/"Пропустить" are reserved control words in the registration
        # flow — an option list whose line equals one becomes unreachable (it triggers cancel /
        # "type your own" instead of being recorded). Warn the admin (the value is still saved).
        if key.endswith("_options"):
            reserved = {"отмена", "другое", "пропустить"}
            clashes = sorted({
                ln.strip() for ln in value.splitlines() if ln.strip().lower() in reserved
            })
            if clashes:
                warning = (
                    "\n\n⚠️ Внимание: варианты "
                    + ", ".join(f"«{html_module.escape(c)}»" for c in clashes)
                    + " совпадают со служебными словами бота и будут недоступны для выбора. "
                    "Переименуйте их."
                )
        if tab_check_failed:
            warning += _tab_check_failed_warning(key)

    if key in _SHEET_TAB_WRITE_MODE:
        await _after_tab_setting_saved(key)

    per_city_base = data.get("per_city_base")
    await state.clear()
    if per_city_base:
        # Phase 09.3 (06, CITY-09): a per-city save/clear returns to the SAME header-aware
        # editor screen, not the general settings landing (RESEARCH Pattern 3 lineage — reuse
        # the FSM, but keep the caller on the screen they actually came from).
        header_code = await admin_selected_city(message.from_user.id)
        text, kb = await _settings_edit_screen(per_city_base, header_code)
        await message.answer(text + warning, parse_mode="HTML", reply_markup=kb)
        return
    # Phase 20 (20-04): возврат на экран ГРУППЫ, с которого менеджер и открыл правку (кнопка
    # «✏️ …» живёт только там). Предупреждение о вариантах/вкладке приклеивается к тексту
    # экрана возврата так же, как раньше приклеивалось к тексту лендинга.
    from handlers.admin_sections import settings_return_screen  # ленивый шов
    text, kb = await settings_return_screen(message.from_user.id, setting_key=key)
    await message.answer(text + warning, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data == "sheets_tab_confirm")
async def sheets_tab_confirm_go(callback: types.CallbackQuery, state: FSMContext):
    """Confirmed overwrite of an existing tab name — saves the pending value (mirrors
    gtconfirm/gtcancel, 09-02: no StateFilter, FSM data is read directly). Returns to the
    «📄 Вкладки таблицы» screen, not the general settings landing — the manager came from there."""
    data = await state.get_data()
    key = data.get("pending_tab_key")
    value = data.get("pending_tab_value")
    await state.clear()
    if key and value is not None:
        await set_setting(key, value)
        if key in _SHEET_TAB_WRITE_MODE:
            await _after_tab_setting_saved(key)
    text = await render_settings_group_text("sheets", callback.from_user.id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_settings_group_keyboard("sheets", callback.from_user.id))
    await callback.answer("✅ Сохранено")


@router.callback_query(F.data == "sheets_tab_cancel")
async def sheets_tab_cancel_go(callback: types.CallbackQuery, state: FSMContext):
    """Cancelled overwrite — nothing saved, prior value untouched."""
    await state.clear()
    text = await render_settings_group_text("sheets", callback.from_user.id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=await build_settings_group_keyboard("sheets", callback.from_user.id))
    await callback.answer("Отменено")


@router.callback_query(F.data == "admin_export_csv")
async def show_admin_export(callback: types.CallbackQuery):
    # Phase 07.2 (CITY-02): CSV export is SCOPED to the admin's selected city — the opposite
    # of the (intentionally unscoped) stats screen. Same resolver every other city-scoped
    # surface uses (_admin_city_view), so module-off collapses to the exact pre-Phase-07.2
    # unfiltered export, byte-identical filename and caption.
    # WR-05: ONE read — the filename must never name a different city than the caption.
    admin_id = callback.from_user.id
    scope, label = await _admin_city_view(admin_id)
    headers, rows = await export_users_csv(city_scope=scope)
    output = io.StringIO()
    writer = csv.writer(output, delimiter=';', quotechar='"', quoting=csv.QUOTE_MINIMAL)
    writer.writerow(headers)
    writer.writerows(rows)
    file_bytes = output.getvalue().encode('utf-8-sig')
    # Filename stays keyed on `scope` (there is no single city code in ALL_CITIES mode — scope
    # is None there too, same as module-off).
    filename = "users.csv" if scope is None else f"users_{scope[0]}.csv"
    # CR-01: the label is an admin-editable free-text setting (`city_label__{code}` in
    # bot_settings) and the bot runs with DefaultBotProperties(parse_mode=HTML), so a caption
    # sent without an explicit parse_mode is parsed as HTML. Escape it exactly like
    # _render_application_card / _render_receipt_card / render_stats_text already do.
    # Phase 09.3 (09.3-02, CITY-08): switched from `scope is None` to `label is None` — module
    # off still has no label (byte-identical caption); ALL_CITIES mode now has a non-None
    # label (ALL_CITIES_LABEL) even though scope is also None, so the caption names the mode.
    caption = (
        "База данных пользователей" if label is None
        else f"База данных пользователей — {html_module.escape(str(label))}"
    )
    document = BufferedInputFile(file_bytes, filename=filename)
    await callback.message.answer_document(document, caption=caption)
    await callback.answer()


@router.callback_query(F.data == "admin_export_incomplete")
async def export_incomplete(callback: types.CallbackQuery):
    await callback.answer("📝 Выгружаю…")
    # Phase 07.1 (CITY-04): incomplete_city_batches() is the SINGLE shared helper for both the
    # manual export and services/scheduler.py:sync_incomplete_sheet_job — headers are computed
    # once inside it (Google Sheets quota) and both callers MUST stay on this helper (WR-01
    # parity), otherwise the 2h auto-sync can silently narrow a tab back down.
    # Phase 07.2 (CITY-02): deliberately NOT scoped to the admin's selected city, unlike
    # show_admin_export above. incomplete_city_batches() writes ALL city tabs in one pass
    # (sync_named_worksheet = clear+rewrite per tab); narrowing to one city here would leave
    # every OTHER city's tab holding stale data after this run. This is already a per-city
    # surface (Phase 07.1, WR-01 parity) — just not filtered by the admin's current selection.
    batches = await incomplete_city_batches()
    total_rows = 0
    written_lines = []
    any_negative = False
    for tab, headers, sheet_rows in batches:
        written = await sync_named_worksheet(tab, headers, sheet_rows)
        total_rows += len(sheet_rows)
        if written < 0:
            any_negative = True
        else:
            written_lines.append(f"«{tab}» — {written}")

    # Aggregate: on which question do dropouts stall most? (works even if the sheet write failed)
    stats = await get_dropout_step_stats()
    total = sum(c for _s, c in stats) or 1
    top = "\n".join(
        f"• {dropout_step_label(step)} — <b>{cnt}</b> ({round(cnt * 100 / total)}%)"
        for step, cnt in stats[:8]
    )
    summary = f"\n\n📊 <b>Где отваливаются:</b>\n{top}" if stats else ""

    if any_negative:
        await callback.message.answer(
            "⚠️ Не удалось записать в таблицу (проверь доступ к Google Sheets). "
            f"Незавершённых регистраций в базе: <b>{total_rows}</b>.{summary}",
            parse_mode="HTML",
        )
        return
    await callback.message.answer(
        f"✅ Обновлено: {', '.join(written_lines)}.{summary}",
        parse_mode="HTML",
    )


from handlers.admin_consent import consent_group_extra_lines, consent_group_extra_buttons, remind_consent_purposes_if_widened, remind_consent_purposes_after_preset  # noqa: E402  -- quick 260822: шов согласий (версия/пересогласие/напоминание о целях)

# ── Seam chain (quick 260822): handlers/admin_settings_lists.py (списочные настройки по
# пунктам) decorates the same admin.router and depends one-way on this module. Imported HERE,
# as the very last statement, so its handlers register right after every handler above at any
# module import order (same device as admin_gamification -> admin_game_tasks). Golden
# snapshot: tests/test_refac_snapshot_260816.py.
from handlers import admin_settings_lists  # noqa: E402,F401

# ── Seam chain (Phase 15, 15-02): handlers/admin_dashboard.py (экран «📊 Дашборд» — тумблеры
# блоков веб-дашборда) decorates the same admin.router. Imported LAST, after
# admin_settings_lists, so its two handlers land right after every handler above at any module
# import order. Golden snapshot: tests/test_refac_snapshot_260816.py.
from handlers import admin_dashboard  # noqa: E402,F401

# ── Seam chain (Phase 19, 19-08): handlers/admin_miniapp.py (экран «🎨 Оформление» Mini App)
# decorates the same admin.router. Imported LAST, after admin_dashboard, so its handlers land
# right after every handler above at any module import order. Golden snapshot:
# tests/test_refac_snapshot_260816.py (regenerated by this plan's task 2, which touches the
# snapshot together with the user_actions.py append).
from handlers import admin_miniapp  # noqa: E402,F401

# ── Seam chain (Phase 19.1, 07, D-20): handlers/admin_miniapp_theme.py (пресеты + ручки
# кастома — второй шов «🎨 Оформление») decorates the same admin.router. Imported LAST, right
# after admin_miniapp, so its handlers land right after every handler above at any module
# import order. Golden snapshot: tests/test_refac_snapshot_260816.py.
from handlers import admin_miniapp_theme  # noqa: E402,F401

# ── Seam chain (Phase 20, 20-01): handlers/admin_sections.py (реестр 8 разделов админки по
# пути делегата и экраны этих разделов) decorates the same admin.router. Imported LAST, right
# after admin_miniapp_theme, so its handler lands right after every handler above at any
# module import order. Golden snapshot: tests/test_refac_snapshot_260816.py.
from handlers import admin_sections  # noqa: E402,F401
