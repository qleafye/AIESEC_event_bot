"""Phase 13 (refac-split-god-files) Plan 01 — safety net BEFORE any handler code moves.

TEST-VALUE-260815.md's "КЛЮЧЕВОЙ ФАКТ": every existing admin/registration test calls a
handler FUNCTION directly, never through the Dispatcher — router registration order, decorator
filters, and middleware attachment are covered by NOTHING. This file closes that gap.

Task 1 — a golden order+filter snapshot of all FOUR routers (admin, payment, registration,
user_actions), walked in the SAME include order main.py uses. Reuses the exact key-derivation
helpers proven in tests/test_roles_phase8.py (`_decorator_lines`/`_keys_from_decorator`/
`_keys_for_handler`) so the snapshot travels with a handler across a file move — only order,
handler name, and derived filter keys are captured, never line numbers or module paths.

(Task 2's Dispatcher feed_update smoke test is appended to this same file by the next commit;
the M1 `is_question_reply` capability-gate regression test lives in its own file,
test_question_reply_gate_260816.py, per the plan's file mapping.)

Drift note (2026-08-18): the plan was authored 2026-08-15 against a smaller admin.py/
registration.py; Phases 14/09.3/7.3 added handlers since (season_*, coinsman_*, city_*,
settings_edit_city*, menu_reset_city*, rereg_start, recall_*, ...). The golden snapshot below
was captured by RUNNING the enumeration helper against CURRENT HEAD (0a76d7e), not transcribed
from the plan — it is authoritative for today's code, not the plan's stale example.
"""
from tests.test_roles_phase8 import _keys_for_handler

from handlers import admin as admin_mod
from handlers import payment as payment_mod
from handlers import registration as registration_mod
from handlers import user_actions as user_actions_mod

# main.py:304-308 — the exact wiring order this whole plan protects. A correct split keeps
# this list byte-for-byte identical (one shared router object per god-file); ANY reorder is a
# T-13-02 threat-register violation.
_ROUTERS_IN_MAIN_ORDER = [
    ("admin", admin_mod),
    ("payment", payment_mod),
    ("registration", registration_mod),
    ("user_actions", user_actions_mod),
]


# ── Task 1: order + filter snapshot ─────────────────────────────────────────────────────────

def _iter_router_handlers(router_name, router_module):
    router = router_module.router
    for observer_name in ("message", "callback_query"):
        observer = getattr(router, observer_name)
        for handler_obj in observer.handlers:
            yield router_name, observer_name, handler_obj


def _build_snapshot_lines():
    """One line per registered handler, in router-include-order then observer.handlers order
    (both authoritative — observer.handlers order IS aiogram's own first-match dispatch order).
    Format: "router|observer|handler_name|key1,key2,..." — no line numbers, no module paths."""
    lines = []
    for router_name, router_module in _ROUTERS_IN_MAIN_ORDER:
        for r_name, observer_name, handler_obj in _iter_router_handlers(router_name, router_module):
            name = handler_obj.callback.__name__
            keys = _keys_for_handler(handler_obj, observer_name)
            lines.append(f"{r_name}|{observer_name}|{name}|{','.join(keys)}")
    return lines


# GOLDEN — captured 2026-08-18 against HEAD 0a76d7e (handlers/admin.py 7868 lines,
# handlers/registration.py 3461 lines) by running _build_snapshot_lines() once against the
# unrefactored code. 292 handlers total across all four routers. Any reorder, dropped handler,
# or changed filter literal changes this list and fails the assert below — that IS the point.
GOLDEN_SNAPSHOT = """
admin|message|cmd_admin_help|cmd:admin
admin|message|cmd_coins|cmd:coins
admin|message|cmd_find_user|cmd:find
admin|message|cmd_create_link|cmd:create_link
admin|message|admin_reply_to_question|
admin|message|cmd_stats|cmd:stats
admin|message|cmd_stats_monthly|cmd:stats_monthly
admin|message|cancel_edit_setting|state:EditSetting:*,state:EditSetting:*
admin|message|cancel_edit_setting|state:EditSetting:*,state:EditSetting:*
admin|message|settings_receive_photo|state:EditSetting:*
admin|message|settings_receive_photo_invalid|state:EditSetting:*
admin|message|settings_receive_file_photo|state:EditSetting:*
admin|message|settings_receive_file_doc|state:EditSetting:*
admin|message|settings_receive_file_invalid|state:EditSetting:*
admin|message|settings_edit_value|state:EditSetting:*
admin|message|cancel_city_form|state:CityForm:*,state:CityForm:*
admin|message|cancel_city_form|state:CityForm:*,state:CityForm:*
admin|message|city_add_label_step|state:CityForm:*
admin|message|city_add_tab_step|state:CityForm:*
admin|message|city_edit_label_step|state:CityForm:*
admin|message|city_edit_tab_step|state:CityForm:*
admin|message|cancel_season_reset|state:SeasonReset:*,state:SeasonReset:*
admin|message|cancel_season_reset|state:SeasonReset:*,state:SeasonReset:*
admin|message|season_reset_name_step|state:SeasonReset:*
admin|message|season_reset_passphrase_step|state:SeasonReset:*
admin|message|cancel_season_import|state:SeasonImport:*,state:SeasonImport:*
admin|message|cancel_season_import|state:SeasonImport:*,state:SeasonImport:*
admin|message|season_import_file_step|state:SeasonImport:*
admin|message|season_import_file_invalid|state:SeasonImport:*
admin|message|season_import_name_step|state:SeasonImport:*
admin|message|cmd_export|cmd:export
admin|message|cmd_broadcast|cmd:broadcast
admin|message|cancel_broadcast|state:Broadcast:*,state:Broadcast:*
admin|message|cancel_broadcast|state:Broadcast:*,state:Broadcast:*
admin|message|process_broadcast|state:Broadcast:*
admin|message|broadcast_schedule_when|state:Broadcast:*
admin|message|broadcast_schedule_message|state:Broadcast:*
admin|message|cmd_scheduled|cmd:scheduled
admin|message|cmd_refresh_allowlist|cmd:refresh_allowlist
admin|message|appr_reject_cancel|state:Approval:*
admin|message|appr_reject_reason|state:Approval:*
admin|message|rcpt_reject_cancel|state:ReceiptReview:*
admin|message|rcpt_reject_reason|state:ReceiptReview:*
admin|message|cmd_settings_guide|cmd:settings_guide
admin|message|roles_add_cancel|state:StaffAdd:*
admin|message|roles_add_person|state:StaffAdd:*
admin|message|cancel_game_task_create|state:GameTaskCreate:*,state:GameTaskCreate:*
admin|message|cancel_game_task_create|state:GameTaskCreate:*,state:GameTaskCreate:*
admin|message|game_task_text_step|state:GameTaskCreate:*
admin|message|game_task_coins_step|state:GameTaskCreate:*
admin|message|game_task_deadline_step|state:GameTaskCreate:*
admin|message|coinsman_cancel_text|state:CoinsManual:*,state:CoinsManual:*
admin|message|coinsman_cancel_text|state:CoinsManual:*,state:CoinsManual:*
admin|message|coinsman_person_step|state:CoinsManual:*
admin|message|coinsman_amount_step|state:CoinsManual:*
admin|message|coinsman_reason_step|state:CoinsManual:*
admin|message|grev_step_cancel|state:GameReview:*,state:GameReview:*
admin|message|grev_step_cancel|state:GameReview:*,state:GameReview:*
admin|message|grev_approve_amount_step|state:GameReview:*
admin|message|grev_reject_reason|state:GameReview:*
admin|callback_query|show_admin_stats|admin_stats
admin|callback_query|show_admin_monthly_stats|admin_monthly_stats
admin|callback_query|show_admin_source_stats|admin_source_stats
admin|callback_query|show_stuck_questions|admin_stuck_questions
admin|callback_query|show_admin_settings|admin_settings
admin|callback_query|show_settings_group|settings_group:*
admin|callback_query|settings_group_noop|settings_group_noop
admin|callback_query|toggle_registration_mode|settings_toggle_reg
admin|callback_query|settings_regmode_reset|settings_regmode_reset
admin|callback_query|settings_regmode_reset_go|settings_regmode_reset_go:*
admin|callback_query|toggle_full_approval|settings_toggle_full_approval
admin|callback_query|toggle_short_approval|settings_toggle_short_approval
admin|callback_query|toggle_party_approval|settings_toggle_party_approval
admin|callback_query|toggle_payment_enabled|toggle_payment_enabled
admin|callback_query|toggle_consent_enabled|toggle_consent_enabled
admin|callback_query|toggle_party_enabled|toggle_party_enabled
admin|callback_query|toggle_party_fork_question|toggle_party_fork_question
admin|callback_query|toggle_payment_reminders|toggle_payment_reminders
admin|callback_query|toggle_uni_mode|toggle_uni_mode
admin|callback_query|toggle_edu_conditional|toggle_edu_conditional
admin|callback_query|toggle_show_progress|toggle_show_progress
admin|callback_query|toggle_notify_mode|settings_toggle_notify
admin|callback_query|toggle_bonus|settings_toggle_bonus
admin|callback_query|settings_file_start|settings_file:*
admin|callback_query|settings_edit_start|settings_edit:*
admin|callback_query|settings_edit_city|settings_edit_city:*
admin|callback_query|settings_reset_city|settings_reset_city:*
admin|callback_query|settings_reset_city_go|settings_reset_city_go:*
admin|callback_query|settings_photo_start|settings_photo:*
admin|callback_query|cancel_edit_setting_callback|settings_cancel
admin|callback_query|sync_sheet|admin_sync_sheet
admin|callback_query|rebuild_sheet_confirm|admin_rebuild_sheet
admin|callback_query|rebuild_sheet|admin_rebuild_sheet_go
admin|callback_query|settings_back_to_admin|settings_back
admin|callback_query|admin_consent_pdfs|admin_consent_pdfs
admin|callback_query|consent_pdf_set|consent_pdf_set:*
admin|callback_query|sheets_tab_confirm_go|sheets_tab_confirm
admin|callback_query|sheets_tab_cancel_go|sheets_tab_cancel
admin|callback_query|show_admin_export|admin_export_csv
admin|callback_query|export_incomplete|admin_export_incomplete
admin|callback_query|show_admin_cities|admin_cities
admin|callback_query|toggle_event_city_enabled|toggle_event_city_enabled
admin|callback_query|city_toggle|city_toggle:*
admin|callback_query|city_default|city_default:*
admin|callback_query|city_add|city_add
admin|callback_query|city_rename_start|city_rename:*
admin|callback_query|city_tab_start|city_tab:*
admin|callback_query|city_delete_confirm|city_del:*
admin|callback_query|city_delete_go|city_del_go:*
admin|callback_query|season_reset_start|admin_season_reset
admin|callback_query|season_reset_go|season_reset_go
admin|callback_query|season_import_start|admin_season_import
admin|callback_query|season_import_go|season_import_go
admin|callback_query|admin_city_switch|admin_city_switch
admin|callback_query|admin_city_pick|admin_city_pick:*
admin|callback_query|admin_menu_root|admin_menu
admin|callback_query|dedupe_sheet_confirm|admin_dedupe_sheet
admin|callback_query|dedupe_sheet_run|admin_dedupe_sheet_go
admin|callback_query|show_admin_broadcast|admin_broadcast
admin|callback_query|process_broadcast_all|broadcast_all
admin|callback_query|process_broadcast_local_file|broadcast_local
admin|callback_query|process_broadcast_unsubscribed|broadcast_unsubscribed
admin|callback_query|process_broadcast_incomplete|broadcast_incomplete
admin|callback_query|cancel_broadcast_callback|broadcast_cancel
admin|callback_query|broadcast_schedule_start|broadcast_schedule
admin|callback_query|sched_cancel|sched_cancel_*
admin|callback_query|broadcast_filter_start|broadcast_filter
admin|callback_query|filter_pick_field|
admin|callback_query|filter_pick_date|filter_f_date
admin|callback_query|filter_pick_date_op|filter_d_after,filter_d_before
admin|callback_query|filter_page_nav|filter_optpage:*
admin|callback_query|filter_pick_value|filter_opt:*
admin|callback_query|filter_back|filter_back
admin|callback_query|filter_count|filter_count
admin|callback_query|filter_send_now|filter_send_now
admin|callback_query|filter_schedule|filter_schedule
admin|callback_query|show_reg_questions|admin_reg_questions
admin|callback_query|toggle_reg_question|reg_q_toggle:*
admin|callback_query|reg_q_track_switch|reg_q_track:*
admin|callback_query|toggle_party_question|reg_q_ptoggle:*
admin|callback_query|toggle_short_question|reg_q_stoggle:*
admin|callback_query|reg_q_noop|reg_q_noop
admin|callback_query|reg_questions_back|reg_q_back
admin|callback_query|admin_event_preset|admin_event_preset
admin|callback_query|preset_apply|preset_apply:*
admin|callback_query|preset_confirm|preset_confirm:*
admin|callback_query|admin_reg_prompts|admin_reg_prompts
admin|callback_query|reg_prompt_track_switch|reg_prompt_track:*
admin|callback_query|reg_prompt_edit|reg_prompt_edit:*
admin|callback_query|show_menu_buttons|admin_menu_buttons
admin|callback_query|toggle_menu_button|menu_toggle:*
admin|callback_query|menu_buttons_back|menu_back
admin|callback_query|menu_reset_city|menu_reset_city
admin|callback_query|menu_reset_city_go|menu_reset_city_go:*
admin|callback_query|show_applications|admin_applications
admin|callback_query|appr_skip|appr_skip:*
admin|callback_query|appr_resume|appr_resume:*
admin|callback_query|appr_approve|appr_approve:*
admin|callback_query|appr_reject_start|appr_reject:*
admin|callback_query|appr_all_confirm|appr_all
admin|callback_query|appr_all_no|appr_all_no
admin|callback_query|appr_all_yes|appr_all_yes*
admin|callback_query|show_receipts|admin_receipts
admin|callback_query|rcpt_confirm|rcpt_confirm:*
admin|callback_query|rcpt_reject_start|rcpt_reject:*
admin|callback_query|rcpt_skip|rcpt_skip:*
admin|callback_query|rcpt_view|rcpt_view:*
admin|callback_query|show_admin_settings_guide|admin_settings_guide
admin|callback_query|show_roles|admin_roles
admin|callback_query|toggle_role_enabled|roles_toggle:*
admin|callback_query|show_role_caps|roles_caps:*
admin|callback_query|toggle_role_cap|roles_cap:*
admin|callback_query|roles_city_start|roles_city:*
admin|callback_query|roles_city_pick|roles_city_pick:*
admin|callback_query|roles_add_start|roles_add
admin|callback_query|roles_assign|roles_addrole:*
admin|callback_query|roles_remove|roles_del:*
admin|callback_query|show_game_tasks|admin_game_tasks
admin|callback_query|show_game_archive|admin_game_archive
admin|callback_query|game_task_archive_go|gtarchive_go:*
admin|callback_query|game_task_unarchive|gtunarchive:*
admin|callback_query|game_task_archive_confirm|gtarchive:*
admin|callback_query|game_task_delete_confirm|gtdelete:*
admin|callback_query|game_task_delete_go|gtdelete_go:*
admin|callback_query|game_task_new|gtnew
admin|callback_query|game_task_category_step|gtcat:*
admin|callback_query|game_task_proof_step|gtproof:*
admin|callback_query|game_task_proof_done|gtproof_done
admin|callback_query|game_task_city_step|gttcity:*
admin|callback_query|game_task_confirm|gtconfirm
admin|callback_query|game_task_create_cancel|gtcancel
admin|callback_query|admin_coins_manual|admin_coins_manual
admin|callback_query|coinsman_cancel_cb|coinsman_cancel
admin|callback_query|coinsman_sign_step|coinsman_sign:*
admin|callback_query|coinsman_confirm|coinsman_confirm
admin|callback_query|admin_coins_journal|admin_coins_journal
admin|callback_query|coinsjrn_page|coinsjrn_page:*
admin|callback_query|coinsjrn_csv|coinsjrn_csv
admin|callback_query|show_game_review|admin_game_review
admin|callback_query|grev_skip|grev_skip:*
admin|callback_query|grev_approve|grev_approve:*
admin|callback_query|grev_approve_custom_start|grev_approve_custom:*
admin|callback_query|grev_reject_start|grev_reject:*
admin|callback_query|sync_game_sheets_confirm|admin_game_sync_sheet
admin|callback_query|sync_game_sheets|admin_game_sync_sheet_go
admin|callback_query|show_game_stats|admin_game_stats
payment|message|process_receipt_document|state:Registration:*
payment|message|process_receipt_photo|state:Registration:*
payment|message|process_receipt_invalid|state:Registration:*
payment|callback_query|process_payment_option|pay_option:*
payment|callback_query|process_pay_later|pay_later
registration|message|process_recall_ignore|state:Registration:*
registration|message|cmd_start|cmd:start
registration|message|cancel_registration|state:Registration:*
registration|message|process_confirm_ok|state:Registration:*
registration|message|process_confirm_edit|state:Registration:*
registration|message|process_resume|state:Registration:*
registration|message|process_resume_text|state:Registration:*
registration|message|process_resume_invalid|state:Registration:*
registration|message|process_date_input|state:Registration:*
registration|message|process_select_input|state:Registration:*
registration|message|process_multi_ignore|state:Registration:*
registration|message|process_ambassador|state:Registration:*
registration|message|process_consent_ignore|state:Registration:*
registration|message|process_full_name|state:Registration:*
registration|message|process_age|state:Registration:*
registration|message|process_email|state:Registration:*
registration|message|process_phone_contact|state:Registration:*
registration|message|process_phone|state:Registration:*
registration|message|process_vk|state:Registration:*
registration|message|process_city|state:Registration:*
registration|message|process_source|state:Registration:*
registration|message|process_local_committee|state:Registration:*
registration|message|process_position|state:Registration:*
registration|message|process_education_status|state:Registration:*
registration|message|process_university|state:Registration:*
registration|message|process_course|state:Registration:*
registration|message|process_specialty|state:Registration:*
registration|message|process_work_status|state:Registration:*
registration|message|process_work_sphere|state:Registration:*
registration|message|process_missing_skills|state:Registration:*
registration|message|process_expectations|state:Registration:*
registration|message|process_informal_day|state:Registration:*
registration|message|process_attendance_format|state:Registration:*
registration|message|process_comments|state:Registration:*
registration|message|process_department|state:Registration:*
registration|message|process_aiesec_role|state:Registration:*
registration|message|process_needs_certificate|state:Registration:*
registration|message|process_english_level|state:Registration:*
registration|message|process_allergies|state:Registration:*
registration|message|process_food_pref|state:Registration:*
registration|message|process_alumni_status|state:Registration:*
registration|message|process_arrival|state:Registration:*
registration|message|process_housing|state:Registration:*
registration|message|process_bed_sharing|state:Registration:*
registration|message|process_bed_partner|state:Registration:*
registration|message|process_transport|state:Registration:*
registration|message|process_cc_shop|state:Registration:*
registration|message|process_exp_organizers|state:Registration:*
registration|message|process_exp_content|state:Registration:*
registration|message|process_volunteer|state:Registration:*
registration|callback_query|recall_keep|recall_keep:*
registration|callback_query|recall_change|recall_change:*
registration|callback_query|party_pick|party_pick:*
registration|callback_query|admin_rereg|admin_rereg
registration|callback_query|rereg_start|rereg_start
registration|callback_query|party_fallback_full|party_fallback_full
registration|callback_query|city_pick|city_pick:*
registration|callback_query|cancel_registration_confirm|reg_cancel_yes
registration|callback_query|cancel_registration_dismiss|reg_cancel_no
registration|callback_query|process_multi_toggle|regmulti:*
registration|callback_query|process_multi_done|regmulti_done:*
registration|callback_query|process_consent_accept|consent_accept:*
user_actions|message|show_my_coins|
user_actions|message|show_leaderboard|
user_actions|message|show_game_tasks|
user_actions|message|cancel_game_submit|state:GameSubmit:*
user_actions|message|receive_proof|state:GameSubmit:*
user_actions|message|upload_receipt_entry|
user_actions|message|show_info_menu|
user_actions|message|show_program|
user_actions|message|show_speakers|
user_actions|message|show_contacts|
user_actions|message|my_referral_link|
user_actions|message|my_referrals|
user_actions|message|ask_organizer_start|
user_actions|message|cancel_question|state:Question:*
user_actions|message|process_question|state:Question:*
user_actions|callback_query|mytask_submit_start|mytask_submit:*
user_actions|callback_query|finalize_game_submission|gs_done
user_actions|callback_query|info_date|info_date
user_actions|callback_query|info_place|info_place
""".strip("\n").splitlines()


def test_handler_order_and_filter_snapshot_matches_golden():
    """Task 1: the core assert. A refactor that keeps ONE shared router object per god-file
    (this phase's chosen split — main.py itself is never touched) leaves this list byte-for-
    byte identical; ANY reorder, dropped handler, or changed filter literal fails here first,
    before a single manual smoke-check is needed."""
    actual = _build_snapshot_lines()
    assert actual == GOLDEN_SNAPSHOT


def test_golden_snapshot_admin_spot_check_order():
    """Acceptance criteria spot-check: cmd_admin_help (the /admin entry point, current name for
    what the plan's stale text calls "cmd_admin") registers before the settings_* screen, which
    registers before the appr_* moderation queue, which registers before admin_game_* — i.e.
    admin.py's top-to-bottom authoring order is preserved in observer.handlers order."""
    admin_lines = [l for l in GOLDEN_SNAPSHOT if l.startswith("admin|")]
    idx_cmd_admin = next(i for i, l in enumerate(admin_lines) if "|cmd_admin_help|" in l)
    idx_settings = next(i for i, l in enumerate(admin_lines) if "|show_admin_settings|" in l)
    idx_appr = next(i for i, l in enumerate(admin_lines) if "|show_applications|" in l)
    idx_game = next(i for i, l in enumerate(admin_lines) if "|show_game_tasks|" in l)
    assert idx_cmd_admin < idx_settings < idx_appr < idx_game


def test_snapshot_total_handler_count_is_292():
    """Second, independent invariant besides content — a handler silently added/removed
    without touching this file's golden text (impossible for a normal edit, but this guards
    against a golden-string typo slipping past review) is caught by count alone."""
    assert len(GOLDEN_SNAPSHOT) == 292
