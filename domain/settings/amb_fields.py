"""Порядок ключей группы реестра `amb` — «🤝 Амбассадоры» (экран группы в боте).

Четыре списка ниже делят тот же набор ключей (ядро, команда, волны, ступени). Порядок
экрана задаёт `AMB_FIELD_ORDER` и не меняется. Токен группы остаётся `amb`.
`wave_rating_show_names`, режим входа, лимит мест и показ баллов на «Моя ссылка» сюда
не входят — это кнопки экранов, не ввод текста.
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
    "delegate_chat_link",  # ссылка на чат города — её бот вставит вместо {chat} в тексты ниже
    "wave_start_message_text", "wave_start_button_text", "wave_deadline_reminder_text", "wave_deadline_reminder_hours",
    "wave_results_announce_text", "wave_results_winner_text", "wave_results_prize_text",
    "wave_rating_header_text", "wave_rating_own_line_text", "wave_rating_closed_text",
    "wave_rating_button_text",  # 10.10: кнопка «🏅 Рейтинг волны» под списком заданий
    "wave_ended_note_text",
    "wave_rating_not_eligible_text",
    # Ссылка и список приглашённых («Моя ссылка») — 09.10 переехали сюда из «🎮 Геймификации».
    "referral_link_prompt_text", "referral_list_header_text", "referral_list_empty_text",
    "amb_invite_threshold",
    "amb_invite_threshold_with_count_text", "amb_invite_threshold_done_with_count_text",
    "amb_invite_threshold_left_text", "amb_invite_threshold_done_text",
    # Тексты амбассадорского блока и пути — делегатские.
    "ambassador_block_header_text",
    "ambassador_path_prompt_text", "ambassador_path_label_invite", "ambassador_path_label_content",
    "ambassador_path_label_none", "ambassador_leave_button_text", "ambassador_leave_confirm_text",
    "ambassador_leave_confirm_yes_button_text",
    "ambassador_leave_confirm_no_button_text",
    "ambassador_leave_done_text",
    # Менеджерский текст конца волны — НЕ переводится, см. i18n_sources._ADMIN_ONLY_GAME_KEYS.
    "wave_end_manager_text",
]

# Деление того же набора. Склейка этих списков порядок экрана не задаёт.
AMB_CORE_FIELDS = (
    "ambassador_referral_coins",
    "amb_invitees_counts_text", "amb_invitee_masked_label_text",
    "amb_referral_reversal_reason_text", "amb_referral_restore_reason_text",
    "amb_referral_catchup_reason_text", "amb_referral_points_text",
    "referral_link_prompt_text", "referral_list_header_text", "referral_list_empty_text",
    "amb_invite_threshold",
    "amb_invite_threshold_with_count_text", "amb_invite_threshold_done_with_count_text",
    "amb_invite_threshold_left_text", "amb_invite_threshold_done_text",
    "ambassador_block_header_text",
    "ambassador_path_prompt_text", "ambassador_path_label_invite", "ambassador_path_label_content",
    "ambassador_path_label_none", "ambassador_leave_button_text", "ambassador_leave_confirm_text",
    "ambassador_leave_confirm_yes_button_text", "ambassador_leave_confirm_no_button_text",
    "ambassador_leave_done_text",
)
AMB_TEAM_FIELDS = (
    "amb_candidate_ack_text", "amb_status_pack_text", "amb_status_no_pack_text",
    "amb_status_candidate_text", "amb_slots_full_text", "amb_taken_text",
    "amb_removed_text", "amb_decline_all_text",
)
AMB_WAVE_FIELDS = (
    "wave_prize_places", "amb_wave_place_text", "delegate_chat_link",
    "wave_start_message_text", "wave_start_button_text", "wave_deadline_reminder_text",
    "wave_deadline_reminder_hours",
    "wave_results_announce_text", "wave_results_winner_text", "wave_results_prize_text",
    "wave_rating_header_text", "wave_rating_own_line_text", "wave_rating_closed_text",
    "wave_rating_button_text", "wave_ended_note_text", "wave_rating_not_eligible_text",
    "wave_end_manager_text",
)
AMB_TIER_FIELDS = (
    "amb_tier1_threshold", "amb_tier2_threshold", "amb_tier3_threshold", "amb_o2o_quota",
    "amb_count_deadline", "amb_tier1_text", "amb_tier2_granted_text", "amb_tier2_waitlist_text",
    "amb_tier3_text", "amb_progress_text", "amb_next_step_o2o_text", "amb_next_step_done_text",
    "amb_next_step_networking_text",
)

_parts = AMB_CORE_FIELDS + AMB_TEAM_FIELDS + AMB_WAVE_FIELDS + AMB_TIER_FIELDS
if sorted(_parts) != sorted(AMB_FIELD_ORDER) or len(_parts) != len(set(_parts)):
    raise RuntimeError("списки ядра, команды, волн и ступеней не покрывают AMB_FIELD_ORDER")
