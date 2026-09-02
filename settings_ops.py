"""Правила настроек — общие для бота и веб-слоя Mini App, БЕЗ aiogram.

Перенесено из `handlers/admin_settings.py` byte-for-byte (Phase 22, план 22-01, D-12):
`_apply_event_type_preset` -> `apply_event_type_preset`, `_SHEET_TAB_WRITE_MODE` ->
`SHEET_TAB_WRITE_MODE`, `HTML_SETTINGS` (имя то же), `_after_tab_setting_saved` ->
`after_tab_setting_saved`, `_per_city_visible_codes` -> `per_city_visible_codes`,
`_base_setting_key` -> `base_setting_key`, `_tab_confirm_text` -> `tab_confirm_text_html`,
`_tab_check_failed_warning` -> `tab_check_failed_warning`. `admin_settings.py` импортирует
отсюда и держит модульные алиасы под старыми приватными именами — тела хендлеров и их
порядок не тронуты (golden-снапшот `test_refac_snapshot_260816.py`).

Причина выноса — `miniapp/` (FastAPI-процесс) не имеет права импортировать aiogram-модуль
(`miniapp/deps.py`: «Модуль aiogram-free»), а `admin_settings.py` стоит на потолке размера
(`tests/test_module_size_convention_260816.py`). Без этого модуля веб-слой фазы 22 либо
тянет за собой бота целиком, либо заводит вторую копию правил — та же формула, что у
соседнего `handlers/settings_validation.py`.

Плюс — четыре карты, ВЫВЕДЕННЫЕ из `SETTINGS_SCHEMA`/`admin_settings.SETTINGS_GROUPS`/
`admin_sections.SECTIONS`, отвечающие на вопрос «что веб даёт править, где это лежит и что
опасно» (D-01/D-02/D-08/D-13): `editable_keys`, `SECTION_GROUPS`, `TOGGLE_SECTION`,
`DANGEROUS_KEYS`, конструктор `item_spec`.

Зависимости — ТОЛЬКО `config`/`settings_schema`/`cities`/`database.db`/`services.sheets`
(тот же `_reset_sheet_cache`, что и раньше), ни одного импорта `aiogram` или `handlers.*`
(сторож `tests/test_settings_ops.py::test_settings_ops_module_does_not_load_aiogram`).
"""
from __future__ import annotations

import html as html_module
import re

from config import config
from cities import PER_CITY_SEP, city_codes, normalize_city
from database.db import get_staff_city, set_setting
from services.sheets import _reset_sheet_cache
from settings_schema import SETTINGS_SCHEMA, get_setting_typed


# ── event_type preset (D-05) ──────────────────────────────────────────────────────────────

async def apply_event_type_preset(event_type: str):
    """D-05: event type presets module flags; each is still manually overridable after.
    conference → payment+consent ON; forum → both OFF; custom → no change."""
    if event_type == "conference":
        await set_setting("payment_enabled", "on")
        await set_setting("consent_enabled", "on")
    elif event_type == "forum":
        await set_setting("payment_enabled", "off")
        await set_setting("consent_enabled", "off")
    # "custom" → no change (manual control)


# ── per-city право на правку (Phase 09.2/09.3) ──────────────────────────────────────────────

async def per_city_visible_codes(admin_id: int) -> list[str]:
    """Which city codes this admin may edit — a RIGHT, not a filter (Phase 07.2 terminology).
    Superadmins (config.ADMIN_IDS) see every city; a manager bound to a city (get_staff_city)
    sees exactly that one; an unbound manager sees all."""
    if admin_id in config.ADMIN_IDS:
        return city_codes()
    bound = await get_staff_city(admin_id)
    if bound:
        return [normalize_city(bound)]
    return city_codes()


# ── HTML-разметка текстовых ключей ────────────────────────────────────────────────────────

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
    # Phase 21 Plan 02 (FORM-SYNC-04): анкета Mini App — три ключа уходят сообщением в чат
    # с parse_mode="HTML" (реестровый текст, не badge/подпись кнопки).
    "reg_sync_from_app_text", "reg_resume_restart_confirm_text", "reg_form_closed_text",
}


def base_setting_key(key: str) -> str:
    """Phase 09.2 (C, CITY-05): strips a `{key}__city__{code}` composite key down to the
    base registry key — used for the HTML_SETTINGS membership check, so per-city text saves
    get the same HTML parsing as the global save."""
    return key.split(PER_CITY_SEP)[0]


# ── Sheets-вкладки: режим записи + confirm-тексты (Quick 260815-3hw) ─────────────────────

# Which Google Sheets tab-name keys the bot actually WRITES to, and HOW. "rewrite" = the sync
# path does ws.clear() + full rewrite (rebuild_main_sheet / sync_named_worksheet); "append" =
# only new rows are ever added (append_to_named_sheet), never a clear. preselect_tab
# (read-only) and the five city_tab_suffix__* keys are deliberately ABSENT — the confirm-gate
# only fires for a key present in this dict.
SHEET_TAB_WRITE_MODE = {
    "main_sheet_tab": "rewrite",
    "incomplete_sheet_tab": "rewrite",
    "game_matrix_tab": "rewrite",
    "game_history_tab": "rewrite",
    "short_sheet_tab": "append",
    "party_sheet_tab": "append",
}


async def after_tab_setting_saved(key: str) -> None:
    """Called after EVERY save/clear of a SHEET_TAB_WRITE_MODE key — resets the cached MAIN
    worksheet handle (services.sheets._sheet global) so a renamed main_sheet_tab takes effect
    on the very next write, no bot restart needed. Named-tab caches need no reset: they're
    keyed BY NAME."""
    if key == "main_sheet_tab":
        _reset_sheet_cache()


def tab_confirm_text_html(key: str, value: str, rows: int) -> str:
    """Confirm-screen body (HTML, для бота) for an EXISTING tab name — text differs by write
    mode (CLAUDE.md: a confirmation has to name the actual damage, and for an append-only tab
    nothing is actually lost)."""
    label = SETTINGS_SCHEMA.get(key, {}).get("label", key)
    safe_value = html_module.escape(value)
    mode = SHEET_TAB_WRITE_MODE.get(key)
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


def tab_check_failed_warning(key: str) -> str:
    """Appended to the post-save confirmation text when tab_row_count() couldn't check the
    spreadsheet at all (Sheets down/unconfigured) — the value is saved regardless, but the
    manager needs to know the existing-tab check didn't run."""
    mode = SHEET_TAB_WRITE_MODE.get(key)
    if mode == "append":
        tail = "если такая вкладка уже есть, бот будет дописывать в неё, ничего не потеряется."
    else:
        tail = "если такая вкладка уже есть, при следующей синхронизации она будет перезаписана."
    return f"\n\n⚠️ Значение сохранено, но проверить вкладку в Google-таблице не удалось — {tail}"


# ── plain_text — снятие HTML-разметки для JSON-ответа веб-слоя (D-06) ────────────────────

_TAG_RE = re.compile(r"<[^>]+>")


def plain_text(value: str) -> str:
    """Снимает HTML-теги бота и разэкранирует сущности — тексты ошибок/подтверждений,
    отдаваемые в JSON веб-слоем (D-06: «plain-text, без HTML бота»). Используется планом
    22-04; здесь только объявляется и покрывается снимком поведения."""
    return html_module.unescape(_TAG_RE.sub("", value))


# ── D-01/D-02/D-08/D-13: что веб даёт править, где это лежит, что опасно ────────────────
#
# Четыре карты ниже ВЫВЕДЕНЫ из SETTINGS_SCHEMA/admin_sections.SECTIONS, а не переписаны
# руками вторым списком (та же формула, что у editable_keys() ниже: один источник правды,
# правка реестра не требует править эту карту отдельно).

# Правка ролей (role_caps_*) из веба вынесена за границу фазы 22 (22-CONTEXT.md § Phase
# Boundary) — у неё свой экран и своя модель прав (T-22-07: элевация через веб-редактор ролей).
EXCLUDED_GROUPS = ("roles",)

# Токены групп в ТОМ ЖЕ порядке, что экраны бота (handlers.admin_settings.SETTINGS_GROUPS) —
# литерал, а не импорт: admin_settings.py тянет aiogram, settings_ops.py — нет (D-12), а
# импортировать оттуда сюда список токенов означало бы либо цикл (admin_settings уже
# импортирует settings_ops), либо протаскивание aiogram транзитивно. Список — девять
# литералов, меняющихся вместе с редкой перестановкой экранов бота, не риск дрейфа кода
# ключей (в отличие от подписей/текста, которые полностью читаются из реестра).
_GROUP_SCREEN_ORDER = (
    "event", "reg", "apps", "sheets", "pay", "party", "consent", "game", "system",
)


def editable_keys() -> tuple[str, ...]:
    """Все ключи SETTINGS_SCHEMA, чья группа не в EXCLUDED_GROUPS (D-01: показываем весь
    реестр, прячем только явно вынесенное за границу фазы). Порядок — сначала ключи групп
    экранов бота (_GROUP_SCREEN_ORDER, паритет с ботом), затем остальные (toggles/
    reg_questions/menu/dashboard/miniapp/...) в порядке реестра."""
    by_group: dict[str | None, list[str]] = {}
    for key, meta in SETTINGS_SCHEMA.items():
        by_group.setdefault(meta.get("group"), []).append(key)

    ordered: list[str] = []
    seen_groups: set[str | None] = set()
    for group in _GROUP_SCREEN_ORDER:
        ordered.extend(by_group.get(group, []))
        seen_groups.add(group)

    for key, meta in SETTINGS_SCHEMA.items():
        group = meta.get("group")
        if group in EXCLUDED_GROUPS or group in seen_groups:
            continue
        ordered.append(key)

    return tuple(ordered)


# Человеческая подпись группы (T-19-45: код ключа/группы человеку не показывается). Для
# групп из SETTINGS_GROUPS бота — те же подписи, что на экранах бота; для групп, которых в
# SETTINGS_GROUPS нет вовсе (reg_questions/menu/dashboard/miniapp — Известное ограничение
# ниже), подписи по формуле экрана-входа, которым они сегодня открываются в боте.
GROUP_LABELS: dict[str, str] = {
    "event": "🎪 Событие/Медиа",
    "reg": "📝 Регистрация",
    "apps": "📋 Заявки",
    "sheets": "📄 Вкладки таблицы",
    "pay": "💳 Оплата",
    "party": "🎉 Party",
    "consent": "📋 Согласия",
    "game": "🎮 Геймификация",
    "system": "🔧 Система",
    "reg_questions": "📋 Вопросы регистрации",
    "menu": "🔘 Кнопки меню",
    "dashboard": "📊 Дашборд",
    "miniapp": "🎨 Mini App",
}

# (section_token, section_label, (group_token, ...)) — в порядке разделов
# handlers.admin_sections.SECTIONS. Раздел «comms» настроек не имеет (пустой раздел не
# рисует заголовка — 22-UI-SPEC.md) и в карте отсутствует.
#
# Известное ограничение (см. SUMMARY): группы menu/dashboard/reg_questions/miniapp не
# объявлены строками ("group", …) в admin_sections.SECTIONS и привязаны к разделам здесь
# ВРУЧНУЮ (menu→event, dashboard→data, reg_questions→form, miniapp→manage) — сторож ниже
# сверяет подписи и порядок разделов, но не принадлежность именно этих четырёх групп
# конкретному разделу; дрейф в боте (если менеджер физически перенесёт кнопку экрана в
# другой раздел) этим тестом не ловится.
SECTION_GROUPS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("event", "🎪 Событие", ("event", "menu")),
    ("form", "📝 Анкета", ("reg_questions", "reg", "party", "consent")),
    ("apps", "📋 Заявки", ("apps",)),
    ("pay", "💳 Оплата", ("pay",)),
    ("game", "🎮 Геймификация", ("game",)),
    ("data", "📊 Данные", ("sheets", "dashboard")),
    ("manage", "🔧 Управление", ("miniapp", "system")),
)

# Ключ группы "toggles" -> раздел, куда его кладёт соответствующая строка ("toggle", …) в
# handlers.admin_sections.SECTIONS (девятнадцать ключей группы "toggles" в SETTINGS_SCHEMA,
# распределены ровно по одному разу — сторож tests/test_settings_ops.py).
TOGGLE_SECTION: dict[str, str] = {
    "reg_bonus_enabled": "event",
    "registration_mode": "form",
    "reg_university_mode": "form",
    "edu_conditional": "form",
    "reg_show_progress": "form",
    "party_enabled": "form",
    "party_fork_question": "form",
    "consent_enabled": "form",
    "consent_recollect_enabled": "form",
    "full_approval": "apps",
    "short_approval": "apps",
    "party_approval": "apps",
    "pending_notify_mode": "apps",
    "preselect_enabled": "apps",
    "pending_reminder_enabled": "apps",
    "nudge_enabled": "apps",
    "payment_enabled": "pay",
    "payment_reminders_enabled": "pay",
    "event_city_enabled": "manage",
}

# Единственный источник «что подтверждаем» для ОБЕИХ поверхностей (UI-SPEC A6): вкладки
# Sheets (пересчёт строк), event_type (пресет модулей), режимы регистрации/модерации, пара
# сегодняшнего DANGER_CONFIRM миниаппа (miniapp/routers/settings.py).
DANGEROUS_KEYS: frozenset[str] = frozenset(SHEET_TAB_WRITE_MODE) | {
    "event_type",
    "registration_mode",
    "full_approval",
    "short_approval",
    "party_approval",
    "miniapp_enabled",
    "miniapp_staff_only",
}


def dangerous_confirm_key(key: str, next_value: str) -> str | None:
    """Ключ реестра с текстом последствий для опасного направления `key -> next_value`, или
    `None`, если текст считается по месту (ключи Sheets-вкладок — `tab_confirm_text_html` +
    `plain_text`) либо направление безопасно (обратное — без подтверждения)."""
    if key in SHEET_TAB_WRITE_MODE:
        return None
    if key == "miniapp_enabled" and next_value == "off":
        return "miniapp_confirm_disable_text"
    if key == "miniapp_staff_only" and next_value == "on":
        return "miniapp_confirm_staff_only_text"
    if key == "registration_mode":
        return "miniapp_settings_confirm_reg_mode_text"
    if key in ("full_approval", "short_approval", "party_approval"):
        return "miniapp_settings_confirm_approval_mode_text"
    if key == "event_type":
        return "miniapp_settings_confirm_event_type_text"
    return None


def next_value_from(key: str, current) -> str:
    """Значение, В КОТОРОЕ уйдёт ключ при смене «из текущего»: для on/off-тумблера —
    противоположное (направление важно: miniapp_enabled -> off опасно, -> on нет), для
    остальных ключей направление не различается — пустая строка."""
    base = base_setting_key(key)
    if SETTINGS_SCHEMA.get(base, {}).get("options") == ["on", "off"]:
        return "off" if current == "on" else "on"
    return ""


async def dangerous_confirm_text(key: str, next_value: str) -> str | None:
    """Plain-текст подтверждения (D-06) для направления `key -> next_value` из реестра, либо
    `None`: направление безопасно или текст считается по месту (вкладки Sheets)."""
    confirm_key = dangerous_confirm_key(base_setting_key(key), next_value)
    if not confirm_key:
        return None
    text = await get_setting_typed(confirm_key)
    return plain_text(str(text)) if text else None


def item_spec(key: str, *, raw: str | None, value, is_default: bool) -> dict:
    """Чистый конструктор словаря одного поля для ответа веб-API — ни одной подписи функция
    не сочиняет, всё из реестра. `search_terms` и per-city счётчики подмешивает роутер
    (планы 22-02/22-04)."""
    base = base_setting_key(key)
    entry = SETTINGS_SCHEMA.get(base, {})
    spec = {
        "key": key,
        "base_key": base,
        "label": entry.get("label", key),
        "type": entry.get("type"),
        "options": entry.get("options"),
        "help": entry.get("prompt"),
        "default": entry.get("default"),
        "value": value,
        "raw": raw,
        "is_default": is_default,
        "per_city": bool(entry.get("per_city")),
        "html": base in HTML_SETTINGS,
        "dangerous": key in DANGEROUS_KEYS or base in DANGEROUS_KEYS,
    }
    max_len = entry.get("max_len")
    if max_len is not None:
        spec["max_len"] = max_len
    return spec
