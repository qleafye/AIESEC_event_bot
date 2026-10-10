"""Тексты, которые бот показывает в чате, а экрана в боте у них не было — правились только в
приложении (на событии без приложения не менялись никак).

Каждая запись — группа настроек бота `(подпись, токен, ключи)`: её экран, правка и поиск
(«🔎 Найти настройку») — общие, как у групп `handlers/admin_settings.SETTINGS_GROUPS`, куда эти
группы и добавляются. В какой раздел админки ведёт кнопка группы — строка `("group", токен)` в
`handlers/admin_sections.SECTIONS`.

Группа реестра (`SETTINGS_SCHEMA[key]["group"]`) у ключей прежняя: от неё зависят корпус
перевода (`services/i18n_sources.DELEGATE_GROUPS`) и раскладка экрана настроек приложения.

Сторож «ключ читает код чата ⇒ его можно поправить в боте» —
`tests/test_chat_keys_reachable_in_bot.py`.
"""

CHAT_TEXT_GROUPS = [
    # Предложение своей реф-ссылки после анкеты — рядом с тумблером «🎁 Предлагать свою ссылку»
    # в «📝 Анкета» (предложение работает и без модуля отбора амбассадоров, у которого свой раздел).
    ("🎁 Предложение своей ссылки", "ref_offer", [
        "miniapp_form_ambassador_offer_heading_text", "miniapp_form_ambassador_offer_body_text",
        "miniapp_form_ambassador_cta_text", "miniapp_form_ambassador_later_text",
        "miniapp_form_ambassador_link_note_text",
        # {section} в пояснении под ссылкой — это подпись раздела приложения.
        "miniapp_hub_referral_label_text",
    ]),
    # Кнопки и ответы анкеты в чате: составные и повторяемые вопросы, справочники, черновик,
    # переход между чатом и приложением.
    ("💬 Анкета в чате: кнопки и ответы", "reg_chat", [
        "reg_composite_chat_check_heading_text", "reg_composite_chat_confirm_button_text",
        "reg_composite_chat_fix_button_text",
        "reg_repeatable_chat_title_prompt_text", "reg_repeatable_chat_description_prompt_text",
        "reg_repeatable_chat_prompt_text", "reg_repeatable_chat_yes_button_text",
        "reg_repeatable_chat_done_button_text",
        "reg_lookup_hint_default_text", "reg_lookup_found_title_text", "reg_lookup_empty_title_text",
        "reg_form_own_chip_text", "reg_form_own_option_text", "reg_form_pick_option_text",
        "reg_form_resume_text_only_text",
        "reg_resume_ttl_hours", "reg_resume_continue_label", "reg_resume_restart_label",
        "reg_resume_restart_confirm_text", "reg_resume_after_restart_text",
        "reg_form_cta_text", "reg_handoff_to_app_text", "reg_handoff_held_by_app_text",
        "reg_handoff_to_bot_label", "reg_handoff_resumed_text", "reg_sync_from_app_text",
        "reg_already_submitted_text", "menu_refreshed_text",
    ]),
    # Пометки и подписи карточки заявки, которую видит менеджер в чате.
    ("🧾 Пометки в карточке заявки", "modcard_labels", [
        "reg_edited_admin_label", "reg_resubmit_admin_label", "reg_prev_reject_admin_label",
        "reg_edit_history_button_label", "miniapp_applications_reject_hint_text",
    ]),
    # Форум: регистрация на месте (анкета в чате у стойки), приглашение волонтёра, лист учебных QR.
    ("🎪 Форум: тексты в чате", "forum_chat", [
        "onsite_reg_intro_text", "onsite_reg_consent_button_text", "onsite_reg_consent_failed_text",
        "onsite_reg_name_prompt_text", "onsite_reg_bad_name_text",
        "onsite_reg_phone_prompt_text", "reg_phone_share_button_text", "onsite_reg_bad_phone_text",
        "onsite_reg_foreign_contact_text",
        "onsite_reg_university_prompt_text", "onsite_reg_skip_button_text",
        "onsite_reg_university_too_long_text",
        "onsite_reg_done_text", "onsite_reg_approved_text", "onsite_reg_closed_text",
        "onsite_reg_already_approved_text", "onsite_reg_existing_text", "onsite_reg_rate_limited_text",
        "volunteer_invite_welcome_text", "volunteer_invite_already_has_access_text",
        "checkin_training_sheet_title_text", "checkin_training_qr_note_text",
        "checkin_training_sheet_caption_text",
    ]),
    # «🔕 Не присылать сегодня» под рассылками-альбомами.
    ("🔕 «Не присылать сегодня»", "broadcast_texts", [
        "broadcast_mute_offer_text", "broadcast_mute_confirm_text", "broadcast_unmute_confirm_text",
    ]),
    # Сообщение и кнопка входа в приложение, текст «приложение выключено», подпись копии резюме.
    ("📱 Приложение: тексты в чате", "miniapp_chat", [
        "miniapp_open_text", "miniapp_open_button", "miniapp_disabled_text",
        "miniapp_upload_caption_resume",
    ]),
]

CHAT_TEXT_KEYS = frozenset(k for _label, _token, keys in CHAT_TEXT_GROUPS for k in keys)
