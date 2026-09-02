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
from settings_schema import SETTINGS_SCHEMA


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
