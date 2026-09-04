"""Phase 19 Plan 01 Task 1 (WEBAPP-01, D-06/D-10): реестр ключей `miniapp_*`, кнопка меню
`menu_miniapp` и константы процесса `miniapp/config.py`.

Список из 45 ключей перечислен ЯВНО: добавление 46-го обязано осознанно ломать этот тест
(контракт, на который опираются экраны планов 19-02..19-08). Расширен планом 19.1-02
(D-03/D-04/D-07/D-08/D-15/D-16/D-18): ручки пресетов оформления, ассеты (стикеры/обложки/
лого тёмной темы/иконка монеты), приветственный экран, тексты пустых состояний менеджера.
Хвост 19.1-06 (D-18): тексты пустой очереди проверки сдач (`miniapp_empty_review`/`_skipped`).

Phase 22 Plan 02 (WEB-SET-02/03, D-15): +41 надпись веб-экрана «⚙️ Настройки» Mini App
(`miniapp_settings_*` из 22-UI-SPEC § Copywriting Contract — 38 UI-текстов, `misc`-заголовок
НЕ заведён, т.к. leftover-группа `_settings_group_keys("misc")` сегодня пуста) + 3 текста
подтверждения опасных ключей (`miniapp_settings_confirm_*`), которых в реестре раньше не
было (требует `settings_ops.dangerous_confirm_key`, план 22-01).
"""
from __future__ import annotations

import re

from handlers import user_actions
from handlers.admin_settings import SETTINGS_FIELDS, SETTINGS_GROUPS
from settings_schema import SETTINGS_SCHEMA

import web_theme
from miniapp import config as miniapp_config

MINIAPP_KEYS = [
    # тумблеры и оформление
    "miniapp_enabled",
    "miniapp_staff_only",
    "miniapp_accent",
    "miniapp_logo",
    # разделы-чекбоксы
    "miniapp_section_tasks",
    "miniapp_section_coins",
    "miniapp_section_leaderboard",
    "miniapp_section_profile",
    "miniapp_section_form",  # Phase 21 Plan 02 (FORM-SYNC-05, D-08): раздел «📝 Анкета»
    "miniapp_section_review",
    "miniapp_section_admin_tasks",
    "miniapp_section_stats",
    "miniapp_section_settings",
    "miniapp_section_applications",  # Phase 23-01 (APP-TINDER-01, D-09): раздел «🗂 Отбор заявок»
    "miniapp_section_questions",  # Quick 260904-2cj (QJRN-01..04): раздел «❓ Вопросы делегатов»
    # тексты
    "miniapp_open_text",
    "miniapp_open_button",
    "miniapp_open_in_bot_text",
    "miniapp_login_button",
    "miniapp_login_hint",
    "miniapp_session_expired_text",
    "miniapp_disabled_text",
    "miniapp_no_access_text",
    "miniapp_upload_too_large_text",
    "miniapp_profile_edit_hint",
    "miniapp_upload_caption_delegate",
    "miniapp_upload_caption_staff",
    "miniapp_upload_caption_settings",  # Quick 260904-8o3 Task 2 (E3): ассет оформления
    "miniapp_upload_caption_resume",  # Phase 21 Plan 02 (FORM-SYNC-05, Pattern 5)
    # 260824-8qw (MD-03): подтверждение перед выключением приложения / скрытием от делегатов
    "miniapp_confirm_disable_text",
    "miniapp_confirm_staff_only_text",
    # Phase 19.1-02: ручки пресетов оформления (D-03/D-04)
    "miniapp_theme_preset",
    "miniapp_theme_secondary",
    "miniapp_theme_bg",
    "miniapp_theme_heading_font",
    "miniapp_theme_playful_tone",
    "miniapp_theme_pattern_enabled",
    # ассеты оформления (D-08/D-15/D-16)
    "miniapp_logo_dark",
    "miniapp_cover",
    "miniapp_cover_dark",
    "miniapp_sticker_empty",
    "miniapp_sticker_success",
    "miniapp_sticker_error",
    "miniapp_sticker_top1",
    "miniapp_coin_icon",
    "miniapp_theme_pattern",  # Phase 23.1: паттерн плиты
    # приветственный экран (D-09)
    "miniapp_onboarding_text",
    "miniapp_onboarding_cta",
    # Phase 23.1-03 (UI-REDESIGN-03): герой и шаги «как это работает» привет-экрана
    "miniapp_onboarding_hero",
    "miniapp_onboarding_steps_title",
    "miniapp_onboarding_steps",
    # Phase 23.1-03 (UI-REDESIGN-02): тексты хаба делегата — плита, факты, надзаголовки, якорь
    "miniapp_hub_balance_eyebrow",
    "miniapp_hub_balance_unit",
    "miniapp_hub_tasks_fact_text",
    "miniapp_hub_days_fact_text",
    "miniapp_hub_countdown_date",
    "miniapp_hub_next_eyebrow",
    "miniapp_hub_sections_eyebrow",
    # Quick 260904-aup Task 1 (UAT D3): плита «Анкета на проверке / Заявка отклонена» над
    # плитками хаба (GET /app/api/hub/status).
    "miniapp_hub_pending_heading_text",
    "miniapp_hub_pending_body_text",
    "miniapp_hub_pending_days",
    "miniapp_hub_rejected_heading_text",
    "miniapp_hub_rejected_body_text",
    "miniapp_hub_rejected_cta_text",
    # Quick 260903: подпись плитки «Дашборд» в хабе менеджера (адрес — cfg.public_url, не реестр)
    "miniapp_tile_dashboard_label",
    # Phase 23.1-05 (UI-REDESIGN-05): подписи профиля делегата по макету 04-profile.png
    "miniapp_profile_contacts_eyebrow",
    "miniapp_profile_form_eyebrow",
    "miniapp_profile_form_progress_text",
    "miniapp_profile_submitted_text",
    "miniapp_profile_edited_text",
    "miniapp_profile_approved_text",  # D-10 (23.1-CONTEXT.md O-2)
    "miniapp_profile_privacy_note",
    # Quick 260904-aup Task 3 (UAT D10): имя-заглушка в плите профиля делегата без анкеты.
    "miniapp_profile_greeting_fallback_text",
    # Phase 23.1-05 (UI-REDESIGN-06): подписи карточки задания по макету 05-task.png
    "miniapp_task_todo_eyebrow",
    "miniapp_task_proof_eyebrow",
    "miniapp_task_proof_note",
    "miniapp_task_deadline_left_text",
    "miniapp_task_review_note",
    # Phase 23.1-06 (UI-REDESIGN-06): подписи плит списочных экранов — задания/монеты/рейтинг
    "miniapp_tasks_plate_eyebrow",
    "miniapp_leaderboard_plate_eyebrow",
    "miniapp_leaderboard_plate_unit",
    # пустые состояния менеджера (D-18) — раньше были литералами в роутерах
    "miniapp_empty_admin_tasks",
    "miniapp_empty_admin_tasks_archived",
    "miniapp_empty_admin_coins",
    # пустые состояния менеджера
    "miniapp_empty_review",
    "miniapp_empty_review_skipped",
    # Phase 23-01 (APP-TINDER-01, D-05/D-06/D-08): экран «🗂 Отбор заявок» Mini App
    "miniapp_empty_applications",
    "miniapp_empty_applications_skipped",
    "miniapp_empty_applications_filtered",
    "miniapp_applications_show_all",
    "miniapp_applications_undo_button",
    "miniapp_applications_approved_toast",
    "miniapp_applications_rejected_toast",
    "miniapp_applications_undone_toast",
    "miniapp_applications_approve_all_confirm",
    "miniapp_applications_reject_no_reason",
    "miniapp_applications_reject_own_reason",
    # Квик 260904-7e7 (D18): шторка отказа — модальный лист, своя кнопка отмены.
    "miniapp_applications_reject_cancel",
    # Phase 23-05 Task 2 (APP-TINDER-03, D-25): подписи карточки заявки, которых не хватало
    # плану 23-04 (API отдавал только то, что зависит от карточки, не статичные подписи кнопок).
    "miniapp_applications_approve_button",
    "miniapp_applications_reject_button",
    "miniapp_applications_resume_open",
    "miniapp_applications_resume_none",
    "miniapp_applications_history_label",
    # Phase 23-05 Task 3 (APP-TINDER-04, D-07/D-08): «Принять всех N», честное «отменить уже
    # нельзя» и три подписи трек-чипов — тоже не было в ответе API 23-04.
    "miniapp_applications_approve_all_button",
    "miniapp_applications_undo_too_late",
    "miniapp_applications_filter_all",
    "miniapp_applications_filter_full",
    "miniapp_applications_filter_party",
    "miniapp_applications_filter_short",
    "miniapp_applications_filter_changed",
    # Quick 260904-2cj (QJRN-01..04): журнал вопросов делегатов Mini App
    "miniapp_empty_questions",
    "miniapp_questions_answer_button",
    "miniapp_questions_sent_toast",
    # Phase 22 Plan 02 (WEB-SET-02/03, D-15): веб-экран «⚙️ Настройки» Mini App
    "miniapp_settings_search_placeholder_text",
    "miniapp_settings_search_count_text",
    "miniapp_settings_search_empty_heading_text",
    "miniapp_settings_search_empty_body_text",
    "miniapp_settings_search_suggest_text",
    "miniapp_settings_value_default_text",
    "miniapp_settings_value_set_text",
    "miniapp_settings_value_not_set_text",
    "miniapp_settings_reset_default_label_text",
    "miniapp_settings_reset_default_confirm_text",
    "miniapp_settings_reset_city_label_text",
    "miniapp_settings_city_own_badge_text",
    "miniapp_settings_city_default_badge_text",
    "miniapp_settings_city_override_count_text",
    "miniapp_settings_city_override_list_text",
    "miniapp_settings_preview_button_text",
    "miniapp_settings_preview_heading_text",
    # Quick 260904-8o3 Task 3 (E5/E6): живая мини-плита превью оформления в настройках.
    "miniapp_settings_theme_preview_heading_text",
    "miniapp_settings_theme_preview_eyebrow_text",
    "miniapp_settings_theme_preview_sub_text",
    "miniapp_settings_batch_bar_text",
    "miniapp_settings_batch_discard_text",
    "miniapp_settings_diff_heading_text",
    "miniapp_settings_diff_was_label_text",
    "miniapp_settings_diff_will_label_text",
    "miniapp_settings_diff_confirm_cta_text",
    "miniapp_settings_diff_confirm_dangerous_cta_text",
    "miniapp_settings_saved_toast_text",
    "miniapp_settings_error_toast_text",
    "miniapp_settings_stale_badge_text",
    "miniapp_settings_stale_current_value_text",
    "miniapp_settings_stale_overwrite_label_text",
    "miniapp_settings_stale_keep_label_text",
    "miniapp_settings_sheets_needs_confirm_text",
    "miniapp_settings_upload_413_text",
    "miniapp_settings_upload_offline_text",
    "miniapp_settings_upload_wrong_type_text",
    "miniapp_settings_forbidden_text",
    "miniapp_settings_loading_text",
    "miniapp_settings_load_error_text",
    "miniapp_settings_dangerous_saved_toast_text",
    "miniapp_settings_confirm_reg_mode_text",
    "miniapp_settings_confirm_approval_mode_text",
    "miniapp_settings_confirm_event_type_text",
    # Phase 22 Plan 07 (D-16): стартовый экран настроек — плитки разделов + общий поиск.
    "miniapp_settings_row_main_label",
    "miniapp_settings_row_rare_label",
    "miniapp_settings_tile_count_text",
    # Phase 22 Plan 07 (D-17 Task 3): заголовки колонок матрицы «трек × вопрос».
    "miniapp_settings_reg_matrix_full_label_text",
    "miniapp_settings_reg_matrix_party_label_text",
    "miniapp_settings_reg_matrix_short_label_text",
]

# Phase 22 Plan 02: новые тексты веб-экрана настроек — используются в проверках ниже.
MINIAPP_SETTINGS_SCREEN_KEYS = [k for k in MINIAPP_KEYS if k.startswith("miniapp_settings_")]

SECTION_KEYS = [k for k in MINIAPP_KEYS if k.startswith("miniapp_section_")]

# Новые ручки этого плана — используются в проверках дефолтов/типов ниже.
THEME_COLOR_KEYS = ["miniapp_theme_secondary", "miniapp_theme_bg"]
THEME_ENUM_KEYS = [
    "miniapp_theme_preset", "miniapp_theme_heading_font",
    "miniapp_theme_playful_tone", "miniapp_theme_pattern_enabled",
]

_ALLOWED_TYPES = {"toggle", "int", "list", "date", "text", "enum", "photo", "file"}


def test_exactly_144_miniapp_keys_and_no_extra():
    # D-17 Task 3: +3 заголовка колонок матрицы «трек × вопрос» (144 -> 147); имя теста
    # осталось историческим (числовые сторожа этого проекта именуются по факту на момент
    # заведения, см. соседние test_module_size_convention_260816.py KNOWN_OVERAGES).
    # Quick 260904-2cj: +4 ключа журнала вопросов делегатов (miniapp_section_questions,
    # miniapp_empty_questions, miniapp_questions_answer_button, miniapp_questions_sent_toast)
    # (147 -> 151).
    # Quick 260904-7e7 (D18): +1 ключ шторки отказа (miniapp_applications_reject_cancel)
    # (151 -> 152).
    # Quick 260904-8o3 Task 2 (E3): +1 ключ подписи ассета оформления
    # (miniapp_upload_caption_settings) — менеджер-делегат больше не получает подпись
    # «копия сдачи» за загрузку из настроек (152 -> 153).
    # Quick 260904-8o3 Task 3 (E5/E6): +3 надписи мини-плиты живого превью оформления
    # (miniapp_settings_theme_preview_heading_text/_eyebrow_text/_sub_text) (153 -> 156).
    # Quick 260904-aup Task 1 (UAT D3): +6 ключей плиты «Анкета на проверке / Заявка
    # отклонена» в хабе (miniapp_hub_pending_heading_text/_body_text/_days,
    # miniapp_hub_rejected_heading_text/_body_text/_cta_text) (156 -> 162).
    # Quick 260904-aup Task 3 (UAT D10): +1 ключ имени-заглушки в плите профиля
    # (miniapp_profile_greeting_fallback_text) (162 -> 163).
    assert len(MINIAPP_KEYS) == 163
    present = sorted(k for k in SETTINGS_SCHEMA if k.startswith("miniapp_"))
    assert present == sorted(MINIAPP_KEYS)


def test_every_miniapp_key_has_label_group_and_valid_type():
    for key in MINIAPP_KEYS:
        entry = SETTINGS_SCHEMA[key]
        assert entry["group"] == "miniapp", key
        assert isinstance(entry["label"], str) and entry["label"].strip(), key
        assert entry["type"] in _ALLOWED_TYPES, key
        # Кодовые значения человеку не показываем (CLAUDE.md): подпись без «miniapp_».
        assert "miniapp_" not in entry["label"], key


def test_toggle_defaults():
    assert SETTINGS_SCHEMA["miniapp_enabled"]["default"] == "off"
    assert SETTINGS_SCHEMA["miniapp_staff_only"]["default"] == "off"
    assert len(SECTION_KEYS) == 11  # Quick 260904-2cj: +miniapp_section_questions (10 -> 11)
    for key in SECTION_KEYS:
        entry = SETTINGS_SCHEMA[key]
        assert entry["type"] == "enum" and entry["options"] == ["on", "off"], key
        assert entry["default"] == "on", key


def test_accent_and_logo_shape():
    accent = SETTINGS_SCHEMA["miniapp_accent"]
    assert accent["type"] == "text"
    assert accent["default"] == "#037EF3"
    assert "#037EF3" in accent["prompt"]  # пример формата в подсказке
    logo = SETTINGS_SCHEMA["miniapp_logo"]
    assert logo["type"] == "photo" and logo["default"] is None


def test_text_keys_have_human_defaults():
    # Phase 23.1-03: miniapp_hub_countdown_date — тоже "text", но per_city-дата без дефолта
    # (пусто = «отсчёта нет», как и у остальных per_city-дат группы event) — исключена тем же
    # приёмом, что и miniapp_accent (не человеческий текст-подпись).
    text_keys = [
        k for k in MINIAPP_KEYS
        if SETTINGS_SCHEMA[k]["type"] == "text"
        and k not in ("miniapp_accent", "miniapp_hub_countdown_date")
    ]
    # D-17 Task 3: +3 заголовка колонок матрицы «трек × вопрос» (116 -> 119).
    # Quick 260904-2cj: +3 текстовых ключа журнала вопросов делегатов (119 -> 122).
    # Quick 260904-7e7 (D18): +1 текстовый ключ шторки отказа (122 -> 123).
    # Quick 260904-8o3 Task 2 (E3): +1 текстовый ключ подписи ассета оформления (123 -> 124).
    # Quick 260904-8o3 Task 3 (E5/E6): +3 текстовых ключа мини-плиты превью оформления (124 -> 127).
    # Quick 260904-aup Task 1 (UAT D3): +5 текстовых ключей плиты «Анкета на проверке / Заявка
    # отклонена» (miniapp_hub_pending_days — "int", не считается здесь) (127 -> 132).
    # Quick 260904-aup Task 3 (UAT D10): +1 текстовый ключ имени-заглушки в плите профиля
    # (132 -> 133).
    assert len(text_keys) == 133
    for key in text_keys:
        default = SETTINGS_SCHEMA[key]["default"]
        assert isinstance(default, str) and default.strip(), key
    assert SETTINGS_SCHEMA["miniapp_open_button"]["default"] == "📱 Открыть приложение"
    assert SETTINGS_SCHEMA["miniapp_login_button"]["default"] == "Войти через Telegram"
    assert SETTINGS_SCHEMA["miniapp_upload_caption_delegate"]["default"] == "копия сдачи"
    assert SETTINGS_SCHEMA["miniapp_upload_caption_staff"]["default"] == "загружено из приложения"
    assert SETTINGS_SCHEMA["miniapp_upload_caption_settings"]["default"] == (
        "🎨 Файл сохранён — можно выбрать его в настройках оформления."
    )


def test_confirm_text_keys_have_human_defaults_and_no_code_leak():
    """260824-8qw (MD-03): текст подтверждения приходит с сервера -- человеческий, называет
    что именно пропадёт, без кодовых значений тумблеров."""
    for key in ("miniapp_confirm_disable_text", "miniapp_confirm_staff_only_text"):
        entry = SETTINGS_SCHEMA[key]
        assert entry["type"] == "text"
        assert entry["group"] == "miniapp"
        assert isinstance(entry["default"], str) and entry["default"].strip()
        assert isinstance(entry["prompt"], str) and entry["prompt"].strip()
        assert "miniapp_enabled" not in entry["default"] and "miniapp_staff_only" not in entry["default"]
    assert "исчезнет у всех" in SETTINGS_SCHEMA["miniapp_confirm_disable_text"]["default"]
    assert "делегат" in SETTINGS_SCHEMA["miniapp_confirm_staff_only_text"]["default"]


def test_theme_color_handles_have_valid_hex_defaults():
    """D-04: 3 цветовые ручки (акцент уже покрыт test_accent_and_logo_shape) — дефолт
    обязан пройти тот же формат `^#rrggbb`, что проверяет `web_theme`/`safe_accent`."""
    hex6 = re.compile(r"^#[0-9A-Fa-f]{6}$")
    for key in THEME_COLOR_KEYS:
        entry = SETTINGS_SCHEMA[key]
        assert entry["type"] == "text", key
        assert hex6.match(entry["default"]), key


def test_theme_enum_handles_default_is_in_options():
    for key in THEME_ENUM_KEYS:
        entry = SETTINGS_SCHEMA[key]
        assert entry["type"] == "enum", key
        assert entry["default"] in entry["options"], key


def test_theme_preset_options_match_web_theme_presets_plus_custom():
    # Сверка с источником (quick 260904-183): реестр не дублирует список пресетов литералом —
    # новый пресет в web_theme.PRESETS без правки options уронит этот тест, а не тихо разойдётся.
    preset = SETTINGS_SCHEMA["miniapp_theme_preset"]
    assert preset["options"] == list(web_theme.PRESETS) + ["custom"]
    assert preset["default"] == "bluebook"


def test_asset_slot_keys_default_to_empty_photo():
    """D-08/D-15/D-16: слоты ассетов оформления пусты по умолчанию — реальные дефолтные
    картинки заводит план 19.1-07, здесь только пустые слоты (T-19.1 boundary)."""
    asset_keys = [
        "miniapp_logo_dark", "miniapp_cover", "miniapp_cover_dark",
        "miniapp_sticker_empty", "miniapp_sticker_success", "miniapp_sticker_error",
        "miniapp_sticker_top1", "miniapp_coin_icon", "miniapp_theme_pattern",
    ]
    for key in asset_keys:
        entry = SETTINGS_SCHEMA[key]
        assert entry["type"] == "photo", key
        assert entry["default"] is None, key


def test_plate_pattern_asset_key_wired_into_theme_and_file_proxy():
    """Phase 23.1-02 (D-05): добавление ключа в `ASSET_KEYS` автоматически даёт и поле в
    `/app/api/me`, и доступ к файлу через `can_read_file` — руками `page.py`/`files.py`
    не правятся, проверяем именно эту проводку."""
    import web_theme
    import settings_ops

    assert web_theme.ASSET_KEYS["plate_pattern_file_id"] == "miniapp_theme_pattern"
    assert "miniapp_theme_pattern" in settings_ops.file_setting_keys()


def test_admin_empty_state_keys_removed_hardcode():
    """D-18: тексты пустых состояний менеджера читаются из реестра, а не из литералов
    в роутерах (см. `grep -rn "empty_text\": \\"" miniapp/routers/` в verification плана)."""
    assert SETTINGS_SCHEMA["miniapp_empty_admin_tasks"]["default"] == "Заданий пока нет."
    assert SETTINGS_SCHEMA["miniapp_empty_admin_tasks_archived"]["default"] == "Архив пуст."
    assert (
        SETTINGS_SCHEMA["miniapp_empty_admin_coins"]["default"]
        == "Ручных операций пока не было."
    )
    assert SETTINGS_SCHEMA["miniapp_empty_review"]["default"] == "Сдач на проверке нет."
    assert (
        SETTINGS_SCHEMA["miniapp_empty_review_skipped"]["default"]
        == "Пропущено всё — осталось {count}."
    )
    assert "{count}" in SETTINGS_SCHEMA["miniapp_empty_review_skipped"]["prompt"]


def test_miniapp_keys_not_in_settings_fields_or_groups():
    """Своя поверхность правки (план 19-08) — иначе ключи всплывут в «📦 Прочие»."""
    field_keys = {k for k, _, _ in SETTINGS_FIELDS}
    group_keys = {k for _, _, keys in SETTINGS_GROUPS for k in keys}
    for key in MINIAPP_KEYS:
        assert key not in field_keys, key
        assert key not in group_keys, key


def test_menu_miniapp_mirrors_menu_game_tasks():
    menu = SETTINGS_SCHEMA["menu_miniapp"]
    ref = SETTINGS_SCHEMA["menu_game_tasks"]
    assert menu["label"] == "📱 Приложение"
    for field in ("type", "group", "options", "default", "per_city", "prompt"):
        assert menu[field] == ref[field], field
    assert set(menu) == set(ref)
    assert menu["group"] == "menu"


def test_menu_miniapp_placed_next_to_menu_game_tasks():
    keys = list(SETTINGS_SCHEMA)
    assert keys.index("menu_miniapp") == keys.index("menu_game_tasks") + 1


def test_limits_match_bot():
    assert miniapp_config.MAX_PARTS == user_actions.MAX_PARTS
    assert miniapp_config.MAX_TEXT_PART == user_actions.MAX_TEXT_PART
    assert miniapp_config.MAX_UPLOAD_BYTES == 20 * 1024 * 1024
    assert miniapp_config.PHOTO_MAX_BYTES == 10 * 1024 * 1024
    assert miniapp_config.INIT_DATA_MAX_AGE == 86400


def test_settings_screen_keys_count_and_shape():
    """Phase 22 Plan 02 (WEB-SET-02/03): 38 UI-текстов экрана настроек + 3 текста
    подтверждения опасных ключей = 41. `misc`-заголовок не заведён, т.к. leftover-группа
    `_settings_group_keys("misc")` сегодня пуста (см. docstring). Phase 22 Plan 07 (D-16):
    +3 текста стартового экрана-плиток (два заголовка ряда + счётчик настроек) = 44.
    Phase 22 Plan 07 (D-17 Task 3): +3 заголовка колонок матрицы «трек × вопрос» = 47.
    Quick 260904-8o3 Task 3 (E5/E6): +3 надписи мини-плиты живого превью оформления = 50."""
    assert len(MINIAPP_SETTINGS_SCREEN_KEYS) == 50
    assert "miniapp_settings_misc_group_label_text" not in MINIAPP_SETTINGS_SCREEN_KEYS
    for key in MINIAPP_SETTINGS_SCREEN_KEYS:
        entry = SETTINGS_SCHEMA[key]
        assert entry["type"] == "text", key
        assert entry["group"] == "miniapp", key
        assert not entry.get("per_city"), key
        assert isinstance(entry["default"], str) and entry["default"].strip(), key
        assert isinstance(entry["prompt"], str) and entry["prompt"].strip(), key
        assert isinstance(entry["label"], str) and entry["label"].strip(), key


def test_settings_screen_confirm_keys_name_real_consequence():
    """22-01 `settings_ops.dangerous_confirm_key` требует эти три ключа — CLAUDE.md: подтверждение
    называет реальный ущерб, а не общую фразу «вы уверены?»."""
    reg = SETTINGS_SCHEMA["miniapp_settings_confirm_reg_mode_text"]["default"]
    approval = SETTINGS_SCHEMA["miniapp_settings_confirm_approval_mode_text"]["default"]
    event_type = SETTINGS_SCHEMA["miniapp_settings_confirm_event_type_text"]["default"]
    assert "форм" in reg
    assert "одобряться" in approval
    assert "Оплата" in event_type and "Согласия" in event_type


def test_settings_screen_keys_no_html_promise():
    """Ни один новый prompt не обещает «поддерживается HTML» — эти ключи не входят в
    HTML_SETTINGS (правило 17.1, `test_html_promise_in_prompt_matches_html_settings`)."""
    for key in MINIAPP_SETTINGS_SCREEN_KEYS:
        assert "html" not in SETTINGS_SCHEMA[key]["prompt"].lower(), key


def test_load_miniapp_config_wraps_dashboard_config():
    cfg = miniapp_config.load_miniapp_config({
        "DASHBOARD_SESSION_SECRET": "s",
        "BOT_TOKEN": "1:x",
        "ADMIN_IDS": "[1, 2]",
    })
    assert isinstance(cfg, miniapp_config.DashboardConfig)
    assert cfg.admin_ids == (1, 2)
    assert cfg.bot_token == "1:x"
