"""Phase 13 (refac-split-god-files) Plan 01 — safety net BEFORE any handler code moves.

TEST-VALUE-260815.md's "КЛЮЧЕВОЙ ФАКТ": every existing admin/registration test calls a
handler FUNCTION directly, never through the Dispatcher — router registration order, decorator
filters, and middleware attachment are covered by NOTHING. This file closes that gap with two
independent nets:

Task 1 — a golden order+filter snapshot of all FOUR routers (admin, payment, registration,
user_actions), walked in the SAME include order main.py uses. Reuses the exact key-derivation
helpers proven in tests/test_roles_phase8.py (`_decorator_lines`/`_keys_from_decorator`/
`_keys_for_handler`) so the snapshot travels with a handler across a file move — only order,
handler name, and derived filter keys are captured, never line numbers or module paths.

Task 2(a) — a feed_update smoke test that builds the REAL Dispatcher (identical include order)
and drives genuine aiogram Update objects through it, proving cross-router first-match routing
(the exact property every direct-call test bypasses). Task 2(b), the M1 `is_question_reply`
capability-gate regression test, lives in its own file (test_question_reply_gate_260816.py) per
the plan's file mapping.

Drift note (2026-08-18): the plan was authored 2026-08-15 against a smaller admin.py/
registration.py; Phases 14/09.3/7.3 added handlers since (season_*, coinsman_*, city_*,
settings_edit_city*, menu_reset_city*, rereg_start, recall_*, ...). The golden snapshot below
was captured by RUNNING the enumeration helper against CURRENT HEAD (0a76d7e), not transcribed
from the plan — it is authoritative for today's code, not the plan's stale example.

Drift note (2026-08-19, quick 260819-gtl, task title + cover photo): 15 handlers appended
(292 -> 307), re-captured by RUNNING `_build_snapshot_lines()` against HEAD after the quick
task's changes -- diffed against the prior 292-line snapshot to confirm every pre-existing
line stayed byte-for-byte identical in the same relative order (pure appends, no reorders):
admin.router gained `game_task_title_step`/`game_task_photo_step`/`game_task_photo_step_invalid`
(GameTaskCreate wizard's new title-first/photo steps), `cancel_game_task_edit`/
`game_task_edittitle_step`/`game_task_editphoto_step`/`game_task_editphoto_invalid`
(new GameTaskEdit point-edit flow), `game_task_photo_skip`/`game_task_edit_screen`/
`game_task_edittitle_start`/`game_task_editphoto_start`/`game_task_removephoto` (new
moderate_game callbacks); user_actions.router gained `mytask_open`/`mytask_back` (the new
delegate task-card open/back-navigation flow).
"""
import asyncio
import time
from contextlib import contextmanager

from aiogram import Bot, Dispatcher
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from tests.test_roles_phase8 import _keys_for_handler, _roles_ready

from handlers import admin as admin_mod
from handlers import payment as payment_mod
from handlers import registration as registration_mod
from handlers import user_actions as user_actions_mod
from handlers.states import Broadcast

ADMIN_ID = 900801
STRANGER_ID = 900804

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
#
# Drift note (2026-08-19, Phase 16-01, GAME-UI-01): 5 handlers appended (307 -> 312),
# re-captured by RUNNING `_build_snapshot_lines()` against HEAD after this plan's changes --
# diffed against the prior 307-line snapshot to confirm every OTHER pre-existing line stayed
# byte-for-byte identical in the same relative order (pure appends + 2 in-place key renames,
# no reorders): user_actions.router gained `gbal_history`/`gbal_top`/`gbal_back` (new «🪙
# Баланс» screen: history pagination / top-10 leaderboard / back-to-summary) and
# `gtasks_page`/`gtasks_noop` (new list-pagination nav row). `mytask_open`/`mytask_back` are
# NOT new handlers -- only their decorator filter literal changed (CONTEXT.md `<specifics>`
# rename `mytask:` -> `gtask_open:`, `mytask_back` -> `gtasks_back:N`), so their derived "keys"
# column changed in place at the SAME position, not appended as a new line.
#
# Drift note (2026-08-20, Phase 16-02, GAME-UI-02): 2 handlers appended (312 -> 314),
# re-captured by RUNNING `_build_snapshot_lines()` against HEAD after this plan's changes and
# diffed against the prior 312-line snapshot -- every pre-existing line byte-for-byte identical
# in the same relative order, no reorders, no key changes: user_actions.router gained
# `gs_remove_last` («🗑 Убрать последнее» -- pop of the FSM draft) and `gs_cancel` (inline
# «❌ Отмена» on the counter message), both GameSubmit.proof-gated, registered right after
# `finalize_game_submission` (gs_done) so the gs_* group stays contiguous. `_game_done_kb` was
# never a router handler -- its removal changes no line.
#
# Drift note (2026-08-20, Phase 16-03, GAME-UI-03): 15 handlers appended (314 -> 329),
# re-captured by RUNNING `_build_snapshot_lines()` against HEAD after this plan's changes and
# diffed against the prior 314-line snapshot -- every pre-existing line byte-for-byte identical
# in the same relative order, no reorders, no key changes (pure appends). All 15 live in the
# NEW seam module handlers/admin_game_tasks.py (imported in admin.py right after
# admin_gamification, so they land last among admin.router's handlers): message observers
# `game_task_editdesc_step`/`game_task_editcoins_step`/`game_task_editdeadline_step`
# (GameTaskEdit text/coins/deadline point-edits); callback observers
# `game_task_editdesc_start`/`game_task_editcoins_start`/`game_task_editdeadline_start`/
# `game_task_editdeadline_preset`/`game_task_editdeadline_custom` (point-edit entries +
# deadline presets), `game_task_preview`/`game_task_preview_close` («👁 Как видит делегат»),
# `game_task_deadline_preset`/`game_task_deadline_custom` (wizard deadline presets),
# `game_task_wizard_edit_menu`/`game_task_wizard_back`/`game_task_wizard_edit_field` (final-step
# «✏️ Изменить» field menu). handlers/game_task_wizard.py (extracted pure helpers) and the
# admin_gamification.py rewrites (list/archive/edit-card/confirm-kb) add or reorder no handler.
#
# Drift note (2026-08-20, Phase 16-04, GAME-UI-03): 1 handler inserted (329 -> 330),
# re-captured by RUNNING `_build_snapshot_lines()` against HEAD and diffed against the prior
# 329-line snapshot -- every pre-existing line byte-for-byte identical, no reorders, no key
# changes. The ONE new line is `coinsman_amount_step_pick` (`coinsman_amount:*`, the quick-pick
# amount buttons of «🪙 Монеты вручную»), registered in admin_gamification.py right after
# `coinsman_amount_step` -- i.e. it lands at its registration position between
# `coinsman_sign_step` and `coinsman_confirm` (an in-place insertion, not a tail append; its
# filter literal is unique, so first-match semantics of every neighbour are unaffected).
# handlers/game_review_render.py (extracted pure renders/keyboards, no router) and the
# seam-import chain change (admin_game_tasks is now imported from admin_gamification's tail,
# not from admin.py -- makes the order identical for every module import order) add or
# reorder no handler.
# Drift note (2026-08-19, quick 260819, preselect landing toggle): 1 handler inserted (330 -> 331),
# re-captured by RUNNING `_build_snapshot_lines()` against HEAD and diffed against the prior
# 330-line snapshot -- every pre-existing line byte-for-byte identical, no reorders, no key
# changes. The ONE new line is `toggle_preselect_enabled` («🎯 Предотбор по таблице» on/off
# button of the «⚙️ Настройки» landing), registered in admin_settings.py right after
# `toggle_party_fork_question` -- an in-place insertion among the sibling module toggles; its
# filter literal is unique (F.data == "toggle_preselect_enabled"), so first-match semantics of
# every neighbour are unaffected.
# Drift note (2026-08-19, quick 260819, coinsman quick-pick catch-all): 1 handler inserted
# (331 -> 332), re-captured by RUNNING `_build_snapshot_lines()` against HEAD and diffed against
# the prior 331-line snapshot -- every pre-existing line byte-for-byte identical, no reorders,
# no key changes. The ONE new line is `coinsman_amount_stale` (`coinsman_amount:*`, NO state
# filter), registered in admin_gamification.py right AFTER `coinsman_amount_step_pick` -- the
# order matters: first-match gives the CoinsManual.amount-gated handler precedence, and the
# catch-all only takes taps outside that state (stale «Или выберите сумму:» keyboard), so no
# neighbour's semantics change.
# Drift note (2026-08-19, quick 260819, scheduler/reminder keys in registry): 2 handlers
# inserted (332 -> 334), re-captured by RUNNING `_build_snapshot_lines()` against HEAD and
# diffed against the prior 332-line snapshot -- every pre-existing line byte-for-byte
# identical, no reorders, no key changes. The two new lines are `toggle_pending_reminder`
# («📋 Сводка о заявках») and `toggle_nudge_enabled` («⏰ Догонялка анкет»), the landing on/off
# buttons for pending_reminder_enabled / nudge_enabled (now declared enum in SETTINGS_SCHEMA),
# registered in admin_settings.py right after `toggle_preselect_enabled` -- in-place insertion
# among the sibling module toggles; both filter literals are unique.
# Drift note (2026-08-22, quick 260822, списочные настройки по пунктам): 5 handlers inserted
# (334 -> 339), re-captured by RUNNING `_build_snapshot_lines()` against HEAD and diffed
# against the prior 334-line snapshot -- every pre-existing line byte-for-byte identical, no
# reorders, no key changes. All five live in the NEW seam module handlers/admin_settings_lists.py
# (imported as the last statement of admin_settings.py, so they land right after its last
# handler, before admin_cities): message observer `settings_list_add_item`
# (EditSetting.waiting_for_list_item -- a NEW state, so `settings_edit_value` on
# waiting_for_value is unaffected) and callback observers `settings_list_add_start`
# («➕ Добавить пункт»), `settings_list_del_pick` («🗑 Удалить пункт» picker),
# `settings_list_rm_go` (tap on an item) and `settings_list_replace_start` («✏️ Заменить список
# целиком»). Filter literals are unique prefixes (settings_list_del: vs settings_list_rm:).
# Drift note (2026-08-22, quick: дайджест сдач геймы): 1 handler inserted (339 -> 340 после слияния с «➕ пункт»),
# re-captured by RUNNING `_build_snapshot_lines()` and diffed against the prior snapshot --
# pure insertion, no reorders. The new line is `toggle_game_submit_notify` (тумблер
# «каждую сдачу отдельно / пачкой» на экране «🎮 Геймификация»), registered in
# admin_gamification.py right after `show_game_review`; the filter literal is unique.
#
# Drift note (2026-08-22, quick consent-versioning): 2 handlers added (340 -> 342 после слияния с «➕ пункт» и дайджестом), re-captured
# by RUNNING `_build_snapshot_lines()` and diffed against the prior 334-line snapshot -- every
# pre-existing line byte-for-byte identical in the same relative order. admin.router gained
# `toggle_consent_recollect` (seam handlers/admin_consent.py, imported at the tail of
# admin_settings.py -> lands right after the settings block, before admin_cities);
# registration.router gained `consent_renew_accept` (seam handlers/reg_consent.py, imported
# after reg_steps -> last in registration.router).
#
# Drift note (2026-08-22, Phase 15-02, D-19 dashboard settings screen): 2 handlers appended
# (362 -> 364), re-captured by RUNNING `_build_snapshot_lines()` against HEAD and diffed
# against the prior 362-line snapshot -- every pre-existing line byte-for-byte identical in
# the same relative order, pure appends, no reorders. The two new lines are
# `open_dashboard_settings` (`admin_dashboard_settings`, opens «📊 Дашборд») and
# `toggle_dashboard_block` (`dash_block:*`, flips one of the eight block toggles), both in the
# NEW seam module handlers/admin_dashboard.py, imported at the tail of admin_settings.py right
# after admin_settings_lists -> land last among admin.router's handlers.
#
# Drift note (2026-08-23, Phase 19-08, D-06/D-10): 13 handlers appended (364 -> 376),
# re-captured by RUNNING `_build_snapshot_lines()` against HEAD and diffed against the prior
# 364-line snapshot -- every pre-existing line byte-for-byte identical in the same relative
# order, pure appends/insertions, no reorders. admin.router gained 12 lines from the NEW seam
# module handlers/admin_miniapp.py («🎨 Оформление» Mini App screen), imported at the tail of
# admin_settings.py right after admin_dashboard: message observers `miniapp_accent_step`/
# `miniapp_logo_step`/`miniapp_logo_step_invalid` (own small FSM group MiniAppTheme, land
# right after admin_settings_lists's `settings_list_add_item` in the message bucket — no
# OTHER message handler moves, since admin_dashboard itself registers none) and callback_query
# observers `open_miniapp_settings`/`toggle_miniapp_enabled`/`toggle_miniapp_staff_only`/
# `toggle_miniapp_section`/`miniapp_edit_accent_start`/`miniapp_edit_logo_start`/
# `miniapp_remove_logo`/`miniapp_cancel_edit` (land right after `toggle_dashboard_block`).
# user_actions.router gained 1 line, `open_miniapp_button` (reply-button «📱 Приложение» ->
# inline web_app), registered at the very tail of handlers/user_actions.py, right after
# `process_question` -> lands last among user_actions.router's message handlers, before the
# gbal_*/gtask_* callback_query block.
#
# Drift note (2026-08-24, Phase 19.1-07, D-20): net +10 handlers (376 -> 386), re-captured by
# RUNNING `_build_snapshot_lines()` against HEAD and diffed against the prior 376-line snapshot
# -- every OTHER pre-existing line byte-for-byte identical in the same relative order (verified
# by diffing both snapshots with every `miniapp` line filtered out first — zero remaining
# diff). Second seam `handlers/admin_miniapp_theme.py` (пресеты BlueBook/YouLead/Своя + ручки
# кастома D-04) imported at the tail of admin_settings.py right after admin_miniapp, replacing
# the old accent/logo edit flow that used to live in admin_miniapp.py itself:
# REMOVED (7, admin_miniapp.py): message `miniapp_accent_step`, `miniapp_logo_step`,
# `miniapp_logo_step_invalid`; callback_query `miniapp_edit_accent_start`,
# `miniapp_edit_logo_start`, `miniapp_remove_logo`, `miniapp_cancel_edit` — accent editing is
# now `miniapp_theme_color:accent` and logo upload/remove is `miniapp_theme_photo:logo` /
# `miniapp_theme_remove_photo:logo` in the new seam, both under the SAME FSM group
# (`MiniAppTheme`, now with 10 fields instead of 2: `logo`, `color`, `logo_dark`, `cover`,
# `cover_dark`, 4× `sticker_*`, `coin_icon`).
# ADDED (17, admin_miniapp_theme.py): message `miniapp_theme_color_step` (one text-input state
# shared by all three color handles, handle carried in `state.get_data()`),
# `miniapp_theme_photo_step`/`miniapp_theme_photo_step_invalid` (one `StateFilter(*9 states)`
# pair covering all nine photo slots — logo×2, cover×2, sticker×4, coin_icon — instead of 9
# near-duplicate per-slot handlers, kept regex-derivable by spelling every `MiniAppTheme.*`
# token out literally on the SAME physical `@router.message(...)` line, since
# `_decorator_lines()` only reads lines starting with `"@router."`); callback_query
# `open_miniapp_theme` (entry from admin_miniapp.py's new «🎭 Пресеты и ручки оформления»
# button), `miniapp_theme_noop` («Своя» marker tap), `miniapp_theme_cancel_edit` (shared
# cancel for every text/photo edit in this seam), `miniapp_preset_pick`/`miniapp_preset_apply`/
# `miniapp_preset_cancel` (preset tap -> preview photo/text fallback -> confirm),
# `miniapp_theme_reset_start`/`miniapp_theme_reset_go` (reset-to-preset confirm), colors
# `miniapp_theme_color_start`, font `miniapp_theme_font_pick`, toggles
# `miniapp_theme_toggle_playful`/`miniapp_theme_toggle_pattern`, and the two prefix-dispatched
# asset entries `miniapp_theme_photo_start`/`miniapp_theme_remove_photo` (cover all nine slots
# via callback_data suffix, same `toggle_miniapp_section`-style dispatch already used one seam
# over). Net: -7 + 17 = +10 lines (376 -> 386).
# Drift note (2026-08-31, Phase 20, 20-01): +1 handler (386 -> 387) -- хвостовой seam-импорт
# handlers/admin_sections.py в конце handlers/admin_settings.py регистрирует один
# `show_admin_section` (admin_sec:*). Снапшот ПЕРЕСНЯТ прогоном `_build_snapshot_lines()`
# против HEAD и сдиффен с прежним 386-строчным: единственное изменение -- вставка одной
# строки после хвоста admin_miniapp_theme (конец цепочки швов admin_settings), все
# существующие строки остались байт-в-байт на прежних местах в прежнем порядке.
# Drift note (2026-09-02, Phase 21, 21-07 Task 1): +1 handler (387 -> 388) -- новый tail-
# хендлер `appr_history` (кнопка «🕓 История» в карточке заявки, D-15) дописан в самый конец
# handlers/admin_moderation.py, поэтому встал сразу после `rcpt_view` (последний хендлер
# этого шва) и перед `show_admin_settings_guide` (первый хендлер следующего шва,
# admin_roles.py). Снапшот ПЕРЕСНЯТ прогоном `_build_snapshot_lines()` и сдиффен с прежним
# 387-строчным -- единственное изменение: одна вставленная строка, всё остальное на прежних
# местах в прежнем порядке.
# Drift note (2026-09-02, Phase 21, 21-07 Task 2): +1 handler (388 -> 389) -- новый
# `toggle_reg_edit_remoderation` («Изменённая анкета — снова на модерацию», D-12) дописан в
# handlers/admin_settings.py сразу после `toggle_nudge_enabled` (тот же принцип, что и у
# соседних тумблеров: физическая позиция декоратора в файле = позиция в этом снапшоте), поэтому
# встал между `toggle_nudge_enabled` и `toggle_payment_reminders`. Снапшот ПЕРЕСНЯТ прогоном
# `_build_snapshot_lines()` и сдиффен с прежним 388-строчным -- единственное изменение: одна
# вставленная строка, всё остальное на прежних местах в прежнем порядке.
# Drift note (2026-09-02, Phase 21, 21-09 Task 3): +3 handlers (389 -> 392) -- новый шов
# handlers/reg_resume.py («▶️ Продолжить с шага N / 🔄 Заново», D-18) импортируется ПОСЛЕДНИМ в
# конце handlers/registration.py (после reg_flow/reg_steps/reg_consent), поэтому его три
# callback_query-хендлера (reg_resume_continue/reg_resume_restart/reg_resume_restart_yes)
# встали в САМЫЙ ХВОСТ registration.router, сразу после `consent_renew_accept` и перед первой
# строкой user_actions.router. Снапшот ПЕРЕСНЯТ прогоном `_build_snapshot_lines()` и сдиффен с
# прежним 389-строчным -- единственное изменение: три вставленные строки подряд, всё остальное
# на прежних местах в прежнем порядке.
# Drift note (2026-09-02, quick 260902-tzh): +5 handlers (392 -> 397) -- карточка заявки по
# единой схеме анкеты: новый tail-хендлер `appr_full` («📄 Полная анкета» при переполнении
# карточки, moderation_card.fit_card) дописан в самый конец handlers/admin_moderation.py сразу
# после `appr_history`; новый шов handlers/admin_modcard.py (экран «🧾 Поля карточки заявки» —
# `modcard_open`/`modcard_toggle`/`modcard_limit`/`modcard_noop`) импортируется ПОСЛЕДНИМ в
# конце handlers/admin_moderation.py (после appr_full), поэтому все пять встали подряд между
# `appr_history` и `show_admin_settings_guide` (первый хендлер следующего шва, admin_roles.py).
# Снапшот ПЕРЕСНЯТ прогоном `_build_snapshot_lines()` и сдиффен с прежним 392-строчным --
# единственное изменение: пять вставленных строк подряд, всё остальное на прежних местах в
# прежнем порядке.
#
# Drift note (quick 260902-vth, 397 -> 400): новый шов `handlers/admin_sheet_logs.py` (экран
# «🕓 Журналы в таблицу», раздел «📊 Данные») импортируется ПОСЛЕДНИМ в хвосте
# handlers/admin_sections.py (после его собственного `show_admin_section`) — три хендлера
# (sheet_logs_open/sheet_logs_autosync_toggle/sheet_logs_sync_go) встали строго между
# `show_admin_section` и `show_admin_cities` (первый хендлер следующего шва). Пересъёмка
# `_build_snapshot_lines()` + diff с прежним 397-строчным подтвердили: чистая вставка трёх
# строк, весь остальной порядок байт-в-байт тот же.
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
admin|message|settings_list_add_item|state:EditSetting:*
admin|message|miniapp_theme_color_step|state:MiniAppTheme:*
admin|message|miniapp_theme_photo_step|state:MiniAppTheme:*
admin|message|miniapp_theme_photo_step_invalid|state:MiniAppTheme:*
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
admin|message|game_task_title_step|state:GameTaskCreate:*
admin|message|game_task_text_step|state:GameTaskCreate:*
admin|message|game_task_photo_step|state:GameTaskCreate:*
admin|message|game_task_photo_step_invalid|state:GameTaskCreate:*
admin|message|game_task_coins_step|state:GameTaskCreate:*
admin|message|game_task_deadline_step|state:GameTaskCreate:*
admin|message|cancel_game_task_edit|state:GameTaskEdit:*,state:GameTaskEdit:*
admin|message|cancel_game_task_edit|state:GameTaskEdit:*,state:GameTaskEdit:*
admin|message|game_task_edittitle_step|state:GameTaskEdit:*
admin|message|game_task_editphoto_step|state:GameTaskEdit:*
admin|message|game_task_editphoto_invalid|state:GameTaskEdit:*
admin|message|coinsman_cancel_text|state:CoinsManual:*,state:CoinsManual:*
admin|message|coinsman_cancel_text|state:CoinsManual:*,state:CoinsManual:*
admin|message|coinsman_person_step|state:CoinsManual:*
admin|message|coinsman_amount_step|state:CoinsManual:*
admin|message|coinsman_reason_step|state:CoinsManual:*
admin|message|grev_step_cancel|state:GameReview:*,state:GameReview:*
admin|message|grev_step_cancel|state:GameReview:*,state:GameReview:*
admin|message|grev_approve_amount_step|state:GameReview:*
admin|message|grev_reject_reason|state:GameReview:*
admin|message|game_task_editdesc_step|state:GameTaskEdit:*
admin|message|game_task_editcoins_step|state:GameTaskEdit:*
admin|message|game_task_editdeadline_step|state:GameTaskEdit:*
admin|message|poll_wizard_cancel|state:PollCreate:*,state:PollCreate:*
admin|message|poll_wizard_cancel|state:PollCreate:*,state:PollCreate:*
admin|message|poll_question_step|state:PollCreate:*
admin|message|poll_options_step|state:PollCreate:*
admin|message|poll_schedule_when|state:PollCreate:*
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
admin|callback_query|toggle_preselect_enabled|toggle_preselect_enabled
admin|callback_query|toggle_pending_reminder|toggle_pending_reminder
admin|callback_query|toggle_nudge_enabled|toggle_nudge_enabled
admin|callback_query|toggle_reg_edit_remoderation|toggle_reg_edit_remoderation
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
admin|callback_query|toggle_consent_recollect|toggle_consent_recollect
admin|callback_query|settings_list_add_start|settings_list_add:*
admin|callback_query|settings_list_del_pick|settings_list_del:*
admin|callback_query|settings_list_rm_go|settings_list_rm:*
admin|callback_query|settings_list_replace_start|settings_list_replace:*
admin|callback_query|open_dashboard_settings|admin_dashboard_settings
admin|callback_query|toggle_dashboard_block|dash_block:*
admin|callback_query|open_miniapp_settings|admin_miniapp_settings
admin|callback_query|toggle_miniapp_enabled|miniapp_toggle_enabled
admin|callback_query|toggle_miniapp_staff_only|miniapp_toggle_staff_only
admin|callback_query|toggle_miniapp_section|miniapp_section:*
admin|callback_query|open_miniapp_theme|miniapp_theme_open
admin|callback_query|miniapp_theme_noop|miniapp_theme_noop
admin|callback_query|miniapp_theme_cancel_edit|miniapp_theme_cancel_edit
admin|callback_query|miniapp_preset_pick|miniapp_preset:*
admin|callback_query|miniapp_preset_apply|miniapp_preset_apply:*
admin|callback_query|miniapp_preset_cancel|miniapp_preset_cancel
admin|callback_query|miniapp_theme_reset_start|miniapp_theme_reset
admin|callback_query|miniapp_theme_reset_go|miniapp_theme_reset_go
admin|callback_query|miniapp_theme_color_start|miniapp_theme_color:*
admin|callback_query|miniapp_theme_font_pick|miniapp_theme_font:*
admin|callback_query|miniapp_theme_toggle_playful|miniapp_theme_toggle_playful
admin|callback_query|miniapp_theme_toggle_pattern|miniapp_theme_toggle_pattern
admin|callback_query|miniapp_theme_photo_start|miniapp_theme_photo:*
admin|callback_query|miniapp_theme_remove_photo|miniapp_theme_remove_photo:*
admin|callback_query|show_admin_section|admin_sec:*
admin|callback_query|sheet_logs_open|sheet_logs_open
admin|callback_query|sheet_logs_autosync_toggle|sheet_logs_autosync_toggle
admin|callback_query|sheet_logs_sync_go|sheet_logs_sync_go
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
admin|callback_query|admin_city_switch|admin_city_switch*
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
admin|callback_query|appr_history|appr_history:*
admin|callback_query|appr_full|appr_full:*
admin|callback_query|modcard_open|modcard_open
admin|callback_query|modcard_toggle|modcard_toggle:*
admin|callback_query|modcard_limit|modcard_limit:*
admin|callback_query|modcard_noop|modcard_noop
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
admin|callback_query|game_task_photo_skip|gtphoto_skip
admin|callback_query|game_task_category_step|gtcat:*
admin|callback_query|game_task_proof_step|gtproof:*
admin|callback_query|game_task_proof_done|gtproof_done
admin|callback_query|game_task_city_step|gttcity:*
admin|callback_query|game_task_confirm|gtconfirm
admin|callback_query|game_task_create_cancel|gtcancel
admin|callback_query|game_task_edit_screen|gtedit:*
admin|callback_query|game_task_edittitle_start|gtedittitle:*
admin|callback_query|game_task_editphoto_start|gteditphoto:*
admin|callback_query|game_task_removephoto|gtremovephoto:*
admin|callback_query|admin_coins_manual|admin_coins_manual
admin|callback_query|coinsman_cancel_cb|coinsman_cancel
admin|callback_query|coinsman_sign_step|coinsman_sign:*
admin|callback_query|coinsman_amount_step_pick|coinsman_amount:*
admin|callback_query|coinsman_amount_stale|coinsman_amount:*
admin|callback_query|coinsman_confirm|coinsman_confirm
admin|callback_query|admin_coins_journal|admin_coins_journal
admin|callback_query|coinsjrn_page|coinsjrn_page:*
admin|callback_query|coinsjrn_csv|coinsjrn_csv
admin|callback_query|show_game_review|admin_game_review
admin|callback_query|toggle_game_submit_notify|toggle_game_submit_notify
admin|callback_query|grev_skip|grev_skip:*
admin|callback_query|grev_approve|grev_approve:*
admin|callback_query|grev_approve_custom_start|grev_approve_custom:*
admin|callback_query|grev_reject_start|grev_reject:*
admin|callback_query|sync_game_sheets_confirm|admin_game_sync_sheet
admin|callback_query|sync_game_sheets|admin_game_sync_sheet_go
admin|callback_query|show_game_stats|admin_game_stats
admin|callback_query|game_task_editdesc_start|gteditdesc:*
admin|callback_query|game_task_editcoins_start|gteditcoins:*
admin|callback_query|game_task_editdeadline_start|gteditdeadline:*
admin|callback_query|game_task_editdeadline_preset|gteditdeadline_preset:*
admin|callback_query|game_task_editdeadline_custom|gteditdeadline_custom
admin|callback_query|game_task_preview|gtpreview:*
admin|callback_query|game_task_preview_close|gtpreview_close
admin|callback_query|game_task_deadline_preset|gtdeadline_preset:*
admin|callback_query|game_task_deadline_custom|gtdeadline_custom
admin|callback_query|game_task_wizard_edit_menu|gtwiz_edit_menu
admin|callback_query|game_task_wizard_back|gtwiz_back
admin|callback_query|game_task_wizard_edit_field|gtwiz_edit:*
admin|callback_query|show_admin_polls|admin_polls
admin|callback_query|show_admin_polls_closed|admin_polls_closed
admin|callback_query|show_poll_card|poll_card:*
admin|callback_query|poll_close_action|poll_close:*
admin|callback_query|poll_export_action|poll_export:*
admin|callback_query|poll_delete_confirm|poll_del:*
admin|callback_query|poll_delete_go|poll_del_go:*
admin|callback_query|poll_new|poll_new
admin|callback_query|poll_cancel_callback|poll_cancel
admin|callback_query|poll_options_done|poll_opts_done
admin|callback_query|poll_toggle_setting|poll_tg_anon,poll_tg_multi
admin|callback_query|poll_settings_next|poll_settings_next
admin|callback_query|poll_audience_pick|poll_aud:*
admin|callback_query|poll_send_now|poll_send_now
admin|callback_query|poll_schedule_start|poll_schedule
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
registration|callback_query|consent_renew_accept|consent_renew:*
registration|callback_query|reg_resume_continue|reg_resume:continue
registration|callback_query|reg_resume_restart|reg_resume:restart
registration|callback_query|reg_resume_restart_yes|reg_resume:restart_yes
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
user_actions|message|open_miniapp_button|
user_actions|callback_query|gbal_history|gbal_history:*
user_actions|callback_query|gbal_top|gbal_top
user_actions|callback_query|gbal_back|gbal_back
user_actions|callback_query|mytask_open|gtask_open:*
user_actions|callback_query|mytask_back|gtasks_back:*
user_actions|callback_query|gtasks_page|gtasks_page:*
user_actions|callback_query|gtasks_noop|gtasks_noop
user_actions|callback_query|mytask_submit_start|mytask_submit:*
user_actions|callback_query|finalize_game_submission|gs_done
user_actions|callback_query|gs_remove_last|gs_remove_last
user_actions|callback_query|gs_cancel|gs_cancel
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
    # quick 260819: +toggle_preselect_enabled, +coinsman_amount_stale, +toggle_pending_reminder/
    # +toggle_nudge_enabled. Опросы (260822): +20 — 5 message (мастер PollCreate, две строки
    # poll_wizard_cancel = два декоратора) в хвост admin.message, 15 callback (список/карточка
    # admin_polls + мастер admin_poll_wizard) в хвост admin.callback_query; чистый аппенд,
    # перепроверен прогоном _build_snapshot_lines() и diff'ом с прежним 334-строчным снапшотом.
    assert len(GOLDEN_SNAPSHOT) == 400  # quick 260819: +toggle_preselect_enabled, +coinsman_amount_stale, +toggle_pending_reminder/+toggle_nudge_enabled; quick 260822: +5 settings_list_*, +toggle_game_submit_notify, +toggle_consent_recollect, +consent_renew_accept; опросы 260822: +20 (342 -> 362 после слияния); Phase 15-02: +2 open_dashboard_settings/toggle_dashboard_block (362 -> 364); Phase 19-08: +12 admin_miniapp.py (message: miniapp_accent_step/miniapp_logo_step/miniapp_logo_step_invalid; callback_query: open_miniapp_settings/toggle_miniapp_enabled/toggle_miniapp_staff_only/toggle_miniapp_section/miniapp_edit_accent_start/miniapp_edit_logo_start/miniapp_remove_logo/miniapp_cancel_edit), +1 user_actions.router open_miniapp_button (364 -> 376); Phase 19.1-07: -7 admin_miniapp.py (accent/logo edit flow replaced) +17 admin_miniapp_theme.py (presets + D-04 handles) = net +10 (376 -> 386); Phase 20-01: +1 admin_sections.py show_admin_section (386 -> 387); Phase 21-07 Task 1: +1 admin_moderation.py appr_history (387 -> 388); Phase 21-07 Task 2: +1 admin_settings.py toggle_reg_edit_remoderation (388 -> 389); Phase 21-09 Task 3: +3 handlers/reg_resume.py (registration.router tail: reg_resume_continue/reg_resume_restart/reg_resume_restart_yes, callback_query) (389 -> 392); quick 260902-tzh: +1 admin_moderation.py appr_full, +4 handlers/admin_modcard.py (modcard_open/modcard_toggle/modcard_limit/modcard_noop) (392 -> 397); quick 260902-vth: +3 handlers/admin_sheet_logs.py (sheet_logs_open/sheet_logs_autosync_toggle/sheet_logs_sync_go), встали сразу после show_admin_section (шов импортируется из хвоста admin_sections.py) и перед show_admin_cities (397 -> 400)


# ── Task 2(a): Dispatcher feed_update smoke — cross-router first-match routing ─────────────
#
# Every existing test (including the harness in test_roles_phase8.py) dispatches through a
# SINGLE router's own `propagate_event` — never through the actual Dispatcher chain main.py
# builds. This is the one place in the suite that proves an Update reaches its intended handler
# THROUGH the real 4-router chain (admin -> payment -> registration -> user_actions), not by
# calling the handler function directly and not by calling one router in isolation.

_DISPATCHER_CACHE: dict = {}


def _full_dispatcher():
    """Built ONCE per test session (module-level singleton routers can only be `include_router`-
    ed into one Dispatcher) with the EXACT include order from main.py:304-308."""
    if "dp" not in _DISPATCHER_CACHE:
        dp = Dispatcher(storage=MemoryStorage())
        payment_mod.init_payment_module(dp.storage)
        dp.include_router(admin_mod.router)
        dp.include_router(payment_mod.router)
        dp.include_router(registration_mod.router)
        dp.include_router(user_actions_mod.router)
        _DISPATCHER_CACHE["dp"] = dp
    return _DISPATCHER_CACHE["dp"]


def _make_message_update(update_id, text, user_id, chat_id=None):
    chat_id = chat_id if chat_id is not None else user_id
    user = User(id=user_id, is_bot=False, first_name="Test")
    chat = Chat(id=chat_id, type="private")
    msg = Message(message_id=update_id, date=int(time.time()), chat=chat, from_user=user, text=text)
    return Update(update_id=update_id, message=msg)


def _make_callback_update(update_id, data, user_id, chat_id=None):
    chat_id = chat_id if chat_id is not None else user_id
    user = User(id=user_id, is_bot=False, first_name="Test")
    chat = Chat(id=chat_id, type="private")
    msg = Message(message_id=update_id, date=int(time.time()), chat=chat, from_user=user, text="stub")
    cb = CallbackQuery(id=str(update_id), from_user=user, chat_instance="test", data=data, message=msg)
    return Update(update_id=update_id, callback_query=cb)


@contextmanager
def _spied(router_module, observer_name, func_name):
    """Replaces a HandlerObject's `.callback` in place (looked up fresh by aiogram's Observer
    on every trigger — see aiogram.dispatcher.event.telegram.TelegramEventObserver.trigger) with
    a recording spy, so the REAL router/filter/middleware chain runs unmodified up to the exact
    point the intended handler's body would start; the body itself never executes (no network,
    no DB side effects), which is exactly what a routing-only smoke test needs."""
    observer = getattr(router_module.router, observer_name)
    handler_obj = next(h for h in observer.handlers if h.callback.__name__ == func_name)
    original = handler_obj.callback
    calls = []

    async def _spy(*args, **kwargs):
        calls.append((args, kwargs))

    handler_obj.callback = _spy
    try:
        yield calls
    finally:
        handler_obj.callback = original


def test_feed_update_smoke_cross_router_first_match(tmp_path):
    _roles_ready(tmp_path)
    dp = _full_dispatcher()
    bot = Bot(token="123456:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
    try:
        # admin.router: callback -- admin_stats is the FIRST callback_query handler registered,
        # proving the admin router (included first) claims its own callback correctly.
        with _spied(admin_mod, "callback_query", "show_admin_stats") as calls:
            result = asyncio.run(dp.feed_update(bot, _make_callback_update(1, "admin_stats", ADMIN_ID)))
            assert result is not UNHANDLED
            assert len(calls) == 1

        # admin.router: command -- /admin.
        with _spied(admin_mod, "message", "cmd_admin_help") as calls:
            result = asyncio.run(dp.feed_update(bot, _make_message_update(2, "/admin", ADMIN_ID)))
            assert result is not UNHANDLED
            assert len(calls) == 1

        # admin.router: callback with a startswith() filter -- appr_approve:1.
        with _spied(admin_mod, "callback_query", "appr_approve") as calls:
            result = asyncio.run(dp.feed_update(bot, _make_callback_update(3, "appr_approve:1", ADMIN_ID)))
            assert result is not UNHANDLED
            assert len(calls) == 1

        # admin.router: state-gated message -- inside the Broadcast wizard, free text must
        # resolve to process_broadcast (not fall through to a sibling router).
        fsm = dp.fsm.resolve_context(bot, chat_id=ADMIN_ID, user_id=ADMIN_ID)
        asyncio.run(fsm.set_state(Broadcast.message))
        try:
            with _spied(admin_mod, "message", "process_broadcast") as calls:
                result = asyncio.run(dp.feed_update(bot, _make_message_update(4, "Всем привет", ADMIN_ID)))
                assert result is not UNHANDLED
                assert len(calls) == 1
        finally:
            asyncio.run(fsm.clear())

        # registration.router: /start -- admin.router (included first, no Command("start")
        # handler) and payment.router (no matching filter either) must NOT intercept it.
        with _spied(registration_mod, "message", "cmd_start") as calls:
            result = asyncio.run(dp.feed_update(bot, _make_message_update(5, "/start", STRANGER_ID)))
            assert result is not UNHANDLED
            assert len(calls) == 1

        # user_actions.router: a plain delegate callback -- last router in the chain, proves the
        # event survives three routers' worth of non-matches before reaching its handler.
        with _spied(user_actions_mod, "callback_query", "info_date") as calls:
            result = asyncio.run(dp.feed_update(bot, _make_callback_update(6, "info_date", STRANGER_ID)))
            assert result is not UNHANDLED
            assert len(calls) == 1
    finally:
        asyncio.run(bot.session.close())
