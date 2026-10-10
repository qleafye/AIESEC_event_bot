"""Порядок ключей группы реестра `amb` — «🤝 Амбассадоры» (экран группы в боте).
Всё амбассадорское — отдельная группа «🤝 Амбассадоры» (раздел админки «🤝 Амбассадоры»; пока
модуль отбора выключен, группа открывается из «🎮 Геймификации»). `wave_rating_show_names` и
режим входа/лимит мест СЮДА не входят — это тумблеры и кнопки экранов раздела (D-29), не ввод текста.
"""

AMB_FIELD_ORDER = [
    # Числа сначала: баллы за приглашённого, призовые места волны.
    "ambassador_referral_coins", "wave_prize_places",
    # Ступени СкиллАп и тексты входа в команду.
    "amb_tier1_threshold", "amb_tier2_threshold", "amb_tier3_threshold", "amb_o2o_quota",
    "amb_count_deadline", "amb_tier1_text", "amb_tier2_granted_text", "amb_tier2_waitlist_text",
    "amb_tier3_text", "amb_progress_text", "amb_next_step_o2o_text", "amb_next_step_done_text",
    "amb_next_step_networking_text", "amb_invitees_counts_text", "amb_invitee_masked_label_text",
    "amb_referral_reversal_reason_text", "amb_referral_restore_reason_text",
    "amb_referral_catchup_reason_text",
    "amb_candidate_ack_text", "amb_status_pack_text", "amb_status_no_pack_text", "amb_referral_points_text",
    "amb_wave_place_text", "amb_status_candidate_text", "amb_slots_full_text", "amb_taken_text",
    "amb_removed_text", "amb_decline_all_text",
    # Тексты старта волны, напоминания, итогов — делегатские, переводятся автоматически.
    "wave_start_message_text", "wave_start_button_text", "wave_deadline_reminder_text", "wave_deadline_reminder_hours",
    "wave_results_announce_text", "wave_results_winner_text", "wave_results_prize_text",
    "wave_rating_header_text", "wave_rating_own_line_text", "wave_rating_closed_text",
    # Ссылка и список приглашённых («Моя ссылка») — 09.10 переехали сюда из «🎮 Геймификации».
    "referral_link_prompt_text", "referral_list_header_text", "referral_list_empty_text",
    # Тексты амбассадорского блока и пути — делегатские.
    "ambassador_block_header_text",
    "ambassador_path_prompt_text", "ambassador_path_label_invite", "ambassador_path_label_content",
    "ambassador_path_label_none", "ambassador_leave_button_text", "ambassador_leave_confirm_text",
    "ambassador_leave_done_text",
    # Менеджерский текст конца волны — НЕ переводится, см. i18n_sources._ADMIN_ONLY_GAME_KEYS.
    "wave_end_manager_text",
]
