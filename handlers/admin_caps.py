"""Phase 8 (ROLE-01/ROLE-02) — capability model.

Single source of truth for "what can this telegram_id do in the admin surface": the fixed
7-value capability set (D-06), the fixed-but-data-driven role registry (D-07), and
`resolve_capabilities()` — the one function every later phase-8 plan (middleware, menu
filtering, notification fan-out) calls to turn a `telegram_id` into a `set[str]` of
capabilities.

D-05 (no cache): every call re-reads SQLite fresh. `staff` is a local ~5-row table (not a
network source like `services/allowlist.py`'s Google Sheets), so caching here would only
introduce "added a manager, doesn't take effect until restart" desync. Do NOT add a memoizing
decorator, a module-level results set, or a manual reload-on-demand helper to this module.

No import from `handlers.admin` here (would create an import cycle — `handlers/admin.py`
imports this module, not the other way around).
"""
from config import config
from database.db import get_staff_roles
from settings_schema import get_setting_typed

# D-06: exactly seven capabilities, in this order. Moderation of applications and receipts
# is intentionally ONE capability (payments are off on YouLead); gamification is separate;
# admins hold all seven via the ADMIN_IDS bootstrap short-circuit below.
ALL_CAPABILITIES = [
    "moderate_reg",
    "moderate_receipts",
    "moderate_game",
    "broadcast",
    "settings",
    "stats",
    "checkin",
]

CAP_LABELS = {
    "moderate_reg": "📋 Модерация заявок",
    "moderate_receipts": "🧾 Модерация чеков",
    "moderate_game": "🎮 Модерация геймификации",
    "broadcast": "📢 Рассылки",
    "settings": "⚙️ Настройки",
    "stats": "📊 Статистика",
    "checkin": "✅ Чек-ин (с Phase 12)",
}

# D-07: roles fixed in code today (admin / reg_manager / game_manager), but the SHAPE is
# data-driven — a fourth role costs exactly one entry here plus two SETTINGS_SCHEMA keys
# (role_caps_<role>/role_<role>_enabled), no refactor. D-12: "admin" deliberately has NO
# entry here and NO registry keys — admin access is config.ADMIN_IDS, un-revocable from the
# bot; see resolve_capabilities()'s bootstrap short-circuit below.
ROLES = {
    "reg_manager": {
        "label": "🛂 Менеджер регистраций",
        "default_caps": ["moderate_reg", "moderate_receipts"],
    },
    "game_manager": {
        "label": "🎮 Менеджер геймификации",
        "default_caps": ["moderate_game"],
    },
}


def role_caps_key(role: str) -> str:
    return f"role_caps_{role}"


def role_enabled_key(role: str) -> str:
    return f"role_{role}_enabled"


async def resolve_capabilities(telegram_id: int) -> set[str]:
    """Fresh SQLite read every call (D-05 — no cache). ADMIN_IDS bootstrap short-circuits to
    the full capability set (D-12/T-08-01) BEFORE touching `staff` or the registry at all —
    an empty or corrupt `staff` table can never lock every admin out."""
    if telegram_id in config.ADMIN_IDS:
        return set(ALL_CAPABILITIES)

    roles = await get_staff_roles(telegram_id)
    caps: set[str] = set()
    for role in roles:
        if role not in ROLES:
            continue  # stale role name left in `staff` after a role was retired from ROLES
        if await get_setting_typed(role_enabled_key(role)) != "on":
            continue  # D-10: role switched off entirely -> contributes zero capabilities
        role_caps = await get_setting_typed(role_caps_key(role)) or []
        # T-08-02: drop anything a manager typed into role_caps_* that isn't a real
        # capability -- a typo in the registry can never grant an out-of-model right.
        caps.update(cap for cap in role_caps if cap in ALL_CAPABILITIES)
    return caps  # D-08: union across every role held


async def has_capability(telegram_id: int, cap: str) -> bool:
    return cap in await resolve_capabilities(telegram_id)


# ── ROLE-01 (D-01/D-02/D-15): the single "event -> required capability" map ─────────────────
#
# One dict serves BOTH the middleware below and (in 08-05) menu assembly -- D-01/D-15's
# explicit "one map" invariant. Key namespaces (see 08-03-PLAN.md <capability_map>):
#   - exact callback_data                      -> "admin_stats"
#   - callback_data prefix (ends with "*")     -> "appr_approve:*"  (matches "appr_approve:123")
#   - a bare slash-command                     -> "cmd:stats"
#   - an FSM-wizard continuation (group only)  -> "state:Broadcast:*"
#   - a predicate-filtered handler, no literal -> "special:question_reply"
# Value "*" (ANY_CAPABILITY) means "any non-empty capability set" -- used only for the two
# admin-panel entry points (admin_menu / cmd:admin), not a real capability.
#
# T-08-12 (deny-by-default, D-02): a callback/command with no entry here resolves to None in
# required_capability(), which the middleware treats as an outright deny -- a new button is
# broken-by-default until someone adds its key, never silently open to everyone.
ANY_CAPABILITY = "*"

ADMIN_CAPS: dict[str, str] = {
    # ── "*" (any capability at all) -- the two admin-panel entry points ────────────────────
    "admin_menu": ANY_CAPABILITY,
    "cmd:admin": ANY_CAPABILITY,

    # ── stats ────────────────────────────────────────────────────────────────────────────
    "admin_export_csv": "stats",
    "admin_export_incomplete": "stats",
    "admin_monthly_stats": "stats",
    "admin_source_stats": "stats",
    "admin_stats": "stats",
    "cmd:export": "stats",
    "cmd:stats": "stats",
    "cmd:stats_monthly": "stats",

    # ── moderate_reg ─────────────────────────────────────────────────────────────────────
    # admin_city_switch/admin_city_pick:* (Phase 07.2, CITY-02) aren't in 08-CONTEXT's D-17
    # command list or 08-RESEARCH's worked capability_map example -- both predate the cities
    # module. They scope the SAME moderation queues (applications/receipts) that
    # `_admin_city_view`'s own comment flags as "the seam Phase 8 plugs into" -- moderate_reg
    # is the closest single capability (D-01's map has no "any of A or B" value shape).
    "admin_applications": "moderate_reg",
    "admin_city_pick:*": "moderate_reg",
    "admin_city_switch": "moderate_reg",
    "appr_all": "moderate_reg",
    "appr_all_no": "moderate_reg",
    # appr_all_yes (CR-02) is matched via F.data.startswith("appr_all_yes"), not "==" -- the
    # button carries an optional ":<city>" suffix. Prefix key, not the bare exact string.
    "appr_all_yes*": "moderate_reg",
    "appr_approve:*": "moderate_reg",
    "appr_reject:*": "moderate_reg",
    "appr_resume:*": "moderate_reg",
    "appr_skip:*": "moderate_reg",
    "cmd:coins": "moderate_reg",
    "cmd:create_link": "moderate_reg",
    "cmd:find": "moderate_reg",
    "special:question_reply": "moderate_reg",
    "state:Approval:*": "moderate_reg",

    # ── moderate_receipts ────────────────────────────────────────────────────────────────
    "admin_receipts": "moderate_receipts",
    "rcpt_confirm:*": "moderate_receipts",
    "rcpt_reject:*": "moderate_receipts",
    "rcpt_skip:*": "moderate_receipts",
    "rcpt_view:*": "moderate_receipts",
    "state:ReceiptReview:*": "moderate_receipts",

    # ── broadcast ────────────────────────────────────────────────────────────────────────
    "admin_broadcast": "broadcast",
    "broadcast_all": "broadcast",
    "broadcast_cancel": "broadcast",
    "broadcast_filter": "broadcast",
    "broadcast_incomplete": "broadcast",
    "broadcast_local": "broadcast",
    "broadcast_schedule": "broadcast",
    "broadcast_unsubscribed": "broadcast",
    "cmd:broadcast": "broadcast",
    "cmd:scheduled": "broadcast",
    "filter_back": "broadcast",
    "filter_count": "broadcast",
    # filter_d_after/filter_d_before (registration-date filter op picker) aren't in
    # 08-RESEARCH's worked capability_map example -- they're matched via
    # F.data.in_({"filter_d_after", "filter_d_before"}), a literal set, not a prefix; same
    # broadcast-filter wizard as everything else under Broadcast.filter_field.
    "filter_d_after": "broadcast",
    "filter_d_before": "broadcast",
    "filter_f_*": "broadcast",
    "filter_f_date": "broadcast",
    "filter_opt:*": "broadcast",
    "filter_optpage:*": "broadcast",
    "filter_schedule": "broadcast",
    "filter_send_now": "broadcast",
    "sched_cancel_*": "broadcast",
    "state:Broadcast:*": "broadcast",

    # ── settings ─────────────────────────────────────────────────────────────────────────
    # admin_cities/toggle_event_city_enabled/city_toggle:* (Phase 07.2, CITY-02/CITY-04) are
    # module-config screens, same shape as the other toggle_*/settings_* config rows below --
    # they predate 08-RESEARCH's worked capability_map example, same as the city-scoping keys
    # filed under moderate_reg above.
    "admin_cities": "settings",
    "admin_consent_pdfs": "settings",
    "admin_dedupe_sheet": "settings",
    "admin_dedupe_sheet_go": "settings",
    "admin_event_preset": "settings",
    "admin_menu_buttons": "settings",
    "admin_reg_prompts": "settings",
    "admin_reg_questions": "settings",
    "admin_rebuild_sheet": "settings",
    "admin_roles": "settings",
    "admin_settings": "settings",
    "admin_settings_guide": "settings",
    "admin_sync_sheet": "settings",
    "city_toggle:*": "settings",
    "cmd:refresh_allowlist": "settings",
    "cmd:settings_guide": "settings",
    "consent_pdf_set:*": "settings",
    "menu_back": "settings",
    "menu_toggle:*": "settings",
    "preset_apply:*": "settings",
    "preset_confirm:*": "settings",
    "reg_prompt_edit:*": "settings",
    "reg_prompt_track:*": "settings",
    "reg_q_back": "settings",
    "reg_q_noop": "settings",
    "reg_q_ptoggle:*": "settings",
    "reg_q_stoggle:*": "settings",
    "reg_q_toggle:*": "settings",
    "reg_q_track:*": "settings",
    "roles_add": "settings",
    "roles_addrole:*": "settings",
    "roles_del:*": "settings",
    "roles_toggle:*": "settings",
    "settings_back": "settings",
    "settings_cancel": "settings",
    "settings_edit:*": "settings",
    "settings_file:*": "settings",
    "settings_group:*": "settings",
    "settings_group_noop": "settings",
    "settings_photo:*": "settings",
    "settings_toggle_bonus": "settings",
    "settings_toggle_full_approval": "settings",
    "settings_toggle_notify": "settings",
    "settings_toggle_party_approval": "settings",
    "settings_toggle_reg": "settings",
    "settings_toggle_short_approval": "settings",
    "state:EditSetting:*": "settings",
    "state:StaffAdd:*": "settings",
    "toggle_consent_enabled": "settings",
    "toggle_edu_conditional": "settings",
    "toggle_event_city_enabled": "settings",
    "toggle_party_enabled": "settings",
    "toggle_party_fork_question": "settings",
    "toggle_payment_enabled": "settings",
    "toggle_payment_reminders": "settings",
    "toggle_show_progress": "settings",
    "toggle_uni_mode": "settings",

    # moderate_game: no keys yet -- the gamification section is Phase 9 (08-CONTEXT domain
    # boundary). checkin: no keys yet -- Phase 12. Both capabilities already exist in
    # ALL_CAPABILITIES/ROLES so a future phase adds handlers, not registry plumbing.
}


def _extract_command(text: str | None) -> str | None:
    """`/coins@bot_username args` -> `"coins"`. Every admin command in this codebase is a bare
    `/word` (Command.prefix defaults to "/", no custom-prefix commands exist here) -- deliberately
    NOT importing aiogram's own Command-matching machinery for this (08-RESEARCH.md Don't
    Hand-Roll table: overkill for the actually-registered command surface)."""
    if not text or not text.startswith("/"):
        return None
    return text.split()[0].lstrip("/").split("@")[0].lower()


def required_capability(*, callback_data: str | None = None, command: str | None = None,
                         raw_state: str | None = None, special: str | None = None) -> str | None:
    """Deny-by-default lookup (D-02). Resolution order: special, raw_state, command,
    callback_data -- the first non-None kwarg supplied wins; no branch ever raises, an
    unresolved lookup is `None` (the caller treats that as an outright deny)."""
    if special is not None:
        return ADMIN_CAPS.get(f"special:{special}")
    if raw_state is not None:
        group = raw_state.split(":", 1)[0]
        return ADMIN_CAPS.get(f"state:{group}:*")
    if command is not None:
        return ADMIN_CAPS.get(f"cmd:{command}")
    if callback_data is not None:
        if callback_data in ADMIN_CAPS:
            return ADMIN_CAPS[callback_data]
        best_key = None
        for key in ADMIN_CAPS:
            if key.endswith("*") and callback_data.startswith(key[:-1]):
                if best_key is None or len(key) > len(best_key):
                    best_key = key
        if best_key is not None:
            return ADMIN_CAPS[best_key]
    return None
