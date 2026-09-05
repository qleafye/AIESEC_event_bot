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
from dataclasses import dataclass

from config import config
from cities import PER_CITY_SEP, city_codes, normalize_city, split_per_city_key
from database.db import delete_setting, get_setting, get_staff_city, set_setting
from services.sheets import _reset_sheet_cache
from settings_schema import SETTINGS_SCHEMA, get_setting_typed
from settings_validation import is_command_like, validate_setting_value


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
    "reg_prompts": "✏️ Тексты вопросов",
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
    ("form", "📝 Анкета", ("reg_questions", "reg_prompts", "reg", "party", "consent")),
    ("apps", "📋 Заявки", ("apps",)),
    ("pay", "💳 Оплата", ("pay",)),
    ("game", "🎮 Геймификация", ("game",)),
    ("data", "📊 Данные", ("sheets", "dashboard")),
    ("manage", "🔧 Управление", ("miniapp", "system")),
)

# Phase 22 Plan 07 (D-16, владелец 03.09): какие разделы стартового экрана настроек попадают
# в первый ряд плиток «Нужно менеджеру» — константа, а не хардкод в JS (веб-слой читает
# `sections[].tier`, ничего не решает сам). Раздел, не попавший сюда, едет во второй ряд
# «Реже» (сегодня — только "manage"; список подрастёт сам, если структура SECTION_GROUPS
# изменится, а этот список забудут поправить — тест ниже сверяет, что каждый код здесь
# реально существует в SECTION_GROUPS).
SETTINGS_MAIN_SECTIONS: frozenset[str] = frozenset({"event", "form", "apps", "pay", "game", "data"})

# Ключ группы "toggles" -> раздел, куда его кладёт соответствующая строка ("toggle", …) в
# handlers.admin_sections.SECTIONS (двадцать один ключ группы "toggles" в SETTINGS_SCHEMA,
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
    "quiet_hours_enabled": "apps",
    "reg_resume_mode": "form",
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

# Phase 22 Plan 07 (D-17 Task 3): трек-переопределения вопросов регистрации (`reg_q_X__party`/
# `reg_q_X__short`) — та же ось, что per-city composite (`cities.per_city_key`), только вместо
# города различает трек анкеты. Ни один из этих составных ключей НЕ объявлен в
# SETTINGS_SCHEMA (как и per-city composite) — `validate_batch_item`/`commit_batch_item` уже
# читают/пишут их как обычную строку (ветка `entry is None` в `validate_setting_value`), здесь
# только распознавание «можно ли вообще их править» (миниапп/routers/settings.py::
# `_editable_target`) и то же множество допустимых базовых ключей, что
# `handlers.admin_reg_config` сверяет по `REG_FLOW` перед записью (T-05-03-02/T-07-09) —
# крафченный `reg_q_noop__party` не долетает до `bot_settings`.
REG_QUESTION_TRACK_SUFFIXES = ("__party", "__short")


def reg_question_track_base(key: str) -> str | None:
    """Базовый ключ `reg_q_*`, если `key` — валидный трек-композит над вопросом анкеты
    (группа `reg_questions`, `type == "toggle"`); иначе `None` (в том числе для суффикса над
    любым другим ключом реестра — тот же гейт, что у бота)."""
    for suffix in REG_QUESTION_TRACK_SUFFIXES:
        if key.endswith(suffix):
            base = key[: -len(suffix)]
            meta = SETTINGS_SCHEMA.get(base)
            if meta and meta.get("type") == "toggle" and meta.get("group") == "reg_questions":
                return base
            return None
    return None


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


# ══ Phase 22 (22-04, D-06/D-08/D-10/D-11): ядро пакетной правки — без FastAPI, без aiogram ═
#
# `validate_batch_item` повторяет порядок девяти проверок `handlers.admin_settings.
# settings_edit_value` (пустое -> команда -> per-city TOCTOU -> validate_setting_value ->
# гейт вкладки / опасный ключ), но без message/state и с plain-текстами (D-06); запись —
# `commit_batch_item` (сброс -> delete_setting, иначе set_setting; event_type -> пресет;
# *_options -> предупреждение о служебных словах; ключ вкладки -> сброс кэша листа).
# Веб-роутер зовёт их по списку ключей в две фазы: сначала ВСЕ проверки, потом ВСЕ записи —
# либо пакет целиком, либо ничего (T-22-02). Второго валидатора нет: `is_command_like` /
# `validate_setting_value` — те же функции, что у бота (T-22-03).

EMPTY_VALUE_TEXT = (
    "Не понял значение — введите его текстом (например: Реги бот). Чтобы очистить настройку, "
    "сбросьте её к значению по умолчанию."
)
CITIES_OFF_TEXT = "Города выключены — правка отменена."
FOREIGN_CITY_TEXT = "Этот город правит суперадмин — правка отменена."
CITY_HEADER_MOVED_TEXT = "Город админки изменился — начните правку заново."

# WR-02: служебные слова потока регистрации — вариант списка, равный одному из них,
# недостижим для выбора (срабатывает отмена / «свой вариант»). Тот же набор, что в боте.
RESERVED_OPTION_WORDS = frozenset({"отмена", "другое", "пропустить"})


def command_like_text(value: str) -> str:
    return (
        f"«{value}» — это команда, а не значение настройки, сохранять её не стал. "
        "Введите значение текстом."
    )


def reserved_option_clashes(value: str) -> list[str]:
    """Строки списка, совпадающие со служебными словами (без учёта регистра); принимает и
    перенос строки, и «;» — оба разделителя читает `settings_schema._parse_setting`."""
    return sorted({
        seg.strip()
        for line in value.splitlines()
        for seg in line.split(";")
        if seg.strip().lower() in RESERVED_OPTION_WORDS
    })


def reserved_words_warning(clashes: list[str]) -> str:
    return (
        "Варианты " + ", ".join(f"«{c}»" for c in clashes)
        + " совпадают со служебными словами бота и будут недоступны для выбора. Переименуйте их."
    )


@dataclass(frozen=True)
class BatchCheck:
    """Итог проверки одного ключа: `value` — нормализованное значение (`None` = сброс);
    ровно одно из `error`/`needs_confirm` может быть непустым; `warning` — не блокирует."""
    value: str | None
    error: str | None = None
    needs_confirm: str | None = None
    warning: str | None = None


def _next_value_for_confirm(key: str, value: str | None) -> str:
    """Направление для `dangerous_confirm_key`: значение, которое ляжет в ключ; при сбросе —
    дефолт реестра (сброс miniapp_enabled -> «off» так же опасен, как явный off)."""
    if value is not None:
        return value
    default = SETTINGS_SCHEMA.get(base_setting_key(key), {}).get("default")
    return "" if default is None else str(default)


async def validate_batch_item(
    key: str,
    value: str | None,
    *,
    visible_codes: list[str],
    selected_city: str | None,
    cities_on: bool,
    tab_probe: tuple[bool, int] | None = None,
    confirmed: bool = False,
) -> BatchCheck:
    """Проверки бота для одного ключа пакета, ни одной записи. `tab_probe` — результат
    `services.sheets.tab_row_count` (роутер зовёт его сам, ядро остаётся синхронным по I/O
    Sheets); `confirmed` — ключ прислан в `confirm`: гейты подтверждения пропускаются."""
    if value is not None:
        value = value.strip()
        if not value:
            return BatchCheck(None, error=EMPTY_VALUE_TEXT)
        if is_command_like(value):
            return BatchCheck(None, error=command_like_text(value))

    if PER_CITY_SEP in key:
        parsed = split_per_city_key(key)
        if parsed is None or not cities_on:
            return BatchCheck(None, error=CITIES_OFF_TEXT)
        _base, code = parsed
        if code not in visible_codes:
            return BatchCheck(None, error=FOREIGN_CITY_TEXT)
        if code != selected_city:
            return BatchCheck(None, error=CITY_HEADER_MOVED_TEXT)

    if value is not None:
        value, error = validate_setting_value(key, value)
        if error:
            return BatchCheck(None, error=plain_text(error))

    if confirmed:
        return BatchCheck(value)

    if key in SHEET_TAB_WRITE_MODE:
        if value is None:
            return BatchCheck(None)  # снятие значения — нечего защищать
        if tab_probe is None:
            return BatchCheck(value, warning=plain_text(tab_check_failed_warning(key)).strip())
        if tab_probe[0]:
            return BatchCheck(value, needs_confirm=plain_text(tab_confirm_text_html(key, value, tab_probe[1])))
        return BatchCheck(value)

    text = await dangerous_confirm_text(key, _next_value_for_confirm(key, value))
    if text:
        return BatchCheck(value, needs_confirm=text)
    return BatchCheck(value)


async def commit_batch_item(key: str, value: str | None) -> str | None:
    """Шаги записи одного ключа (после того как ВЕСЬ пакет прошёл проверки). Возвращает
    предупреждение (не блокирующее) либо `None`."""
    warning = None
    if value is None:
        await delete_setting(key)
    else:
        await set_setting(key, value)
        if key == "event_type":
            await apply_event_type_preset(value.strip().lower())
        if key.endswith("_options"):
            clashes = reserved_option_clashes(value)
            if clashes:
                warning = reserved_words_warning(clashes)
    if key in SHEET_TAB_WRITE_MODE:
        await after_tab_setting_saved(key)
    return warning


# ══ Phase 22 (22-04, D-07): превью текста глазами делегата ════════════════════════════════
#
# Консьюмеры подставляют плейсхолдеры цепочками `.replace` (НЕ `.format`: текст менеджера
# может содержать посторонние `{}`) — grep по handlers/services/miniapp: {deadline} {season}
# {requisites} {reason} {penalties} {option} {label} {display} {delta} {count} {balance}
# {amount} {total} {step} {date}. Превью делает то же самое с образцами: реальные значения
# берутся из реестра/БД (`preview_samples`), недоступные — человеческие заглушки.

# Плейсхолдеры, которые подставляет АДРЕСАТ текста — экран настроек Mini App (счётчики
# поиска, diff, шапка города), а не бот делегату. Превью их не трогает; сторож в
# tests/test_miniapp_settings_batch.py требует, чтобы каждый плейсхолдер реестра был либо
# здесь, либо в PREVIEW_SAMPLES.
PREVIEW_ADDRESSEE_PLACEHOLDERS: frozenset[str] = frozenset({
    "shown", "suggestions", "cities", "default", "value", "tab", "rows", "consequence",
})

PREVIEW_SAMPLES: dict[str, str] = {
    "name": "Иван",
    "season": "Юлид'25",
    "deadline": "15.10.2026",
    "requisites": "Сбербанк, 2202 2000 0000 0000, Иван И.",
    "amount": "3500",
    "option": "Полная форма",
    "penalties": "⚠️ Штрафы за отмену:\n• до 10.10.2026 — остаток 1000 ₽\n\n",
    "label": "Университет",
    "display": "МГУ",
    "delta": "+10",
    "reason": "за активность на форуме",
    "balance": "120",
    "count": "3",
    "total": "12",
    "step": "4",
    "date": "02.09.2026 14:30",
    "rank": "5",
    "link": "https://t.me/YouLead_bot?start=ref_123",
    "breakdown": "📷 1 фото, ✍️ 1 текст",
    "page": "1",
    "status": "на проверке",
    "days": "12",
    "done": "3",
    "filled": "14",
    "n": "10",
    # Quick 260904-dq1: {time} у quiet_hours_manager_notice_text — конец окна тихих часов,
    # подставляется services.quiet_hours.manager_notice тем же .replace-приёмом.
    "time": "09:00",
}


async def preview_samples() -> dict[str, str]:
    """Образцы для превью: поверх заглушек — реальные значения мероприятия из реестра/БД
    (сезон, дедлайн оплаты, реквизиты), чтобы менеджер видел свой текст, а не абстрактный."""
    samples = dict(PREVIEW_SAMPLES)
    season = await get_setting_typed("event_season")
    if season:
        samples["season"] = str(season)
    deadline = await get_setting("payment_deadline")
    if deadline:
        samples["deadline"] = deadline.split()[0]
    requisites = await get_setting_typed("payment_requisites")
    if requisites:
        samples["requisites"] = str(requisites)
    return samples


def preview_text(key: str, value: str, *, samples: dict[str, str]) -> str:
    """Текст, как его увидит делегат: подстановка образцов теми же `.replace`-цепочками, что у
    консьюмеров; для ключей HTML_SETTINGS разметка снимается (`plain_text`) — в DOM экрана
    едет только текст (D-06/D-07)."""
    text = value
    for name, sample in samples.items():
        text = text.replace("{" + name + "}", sample)
    if base_setting_key(key) in HTML_SETTINGS:
        return plain_text(text)
    return text


# ══ Phase 22 (22-04, D-05, T-22-12): фото/файлы настроек через существующий staff-путь ═════

def file_setting_keys() -> tuple[str, ...]:
    """Правимые ключи типа photo/file — их значения (file_id) менеджер с правом `settings`
    вправе читать через GET /app/api/file/{file_id}, и только их."""
    return tuple(
        key for key in editable_keys()
        if SETTINGS_SCHEMA[key].get("type") in ("photo", "file")
    )


async def is_current_file_value(file_id: str) -> bool:
    """`file_id` прямо сейчас — значение какого-либо photo/file-ключа реестра (проверка по
    bot_settings, не по произвольному file_id — иначе право `settings` стало бы чтением любых
    файлов бота)."""
    if not file_id:
        return False
    for key in file_setting_keys():
        if await get_setting(key) == file_id:
            return True
    return False
