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
import logging

from aiogram import BaseMiddleware
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.types import CallbackQuery, Message, TelegramObject

from config import config
from database.db import get_staff_roles, get_staff_ids_by_role, get_staff_city
from settings_schema import get_setting_typed
# Phase 09.2 (D): city filter for capability_holders/notify_by_capability. cities.py imports
# only config/database.db/settings_schema (see cities.py's own module docstring) -- it never
# imports handlers.*, so importing it here from handlers/admin_caps.py cannot form a cycle.
from cities import cities_module_on, normalize_city

logger = logging.getLogger(__name__)

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


# ── D-13: notification fan-out by capability ────────────────────────────────────────────────
#
# Three sites (new application/registration.py, new receipt/payment.py, delegate question/
# user_actions.py) route to whoever HOLDS the relevant capability, not the bare ADMIN_IDS
# list -- a reg_manager who isn't in config.ADMIN_IDS must still see new applications. Three
# technical-failure sites (services/sheets.py, services/scheduler.py, services/reminders.py)
# deliberately keep the old `for admin_id in config.ADMIN_IDS` shape -- D-13 explicitly does
# NOT route those to capability holders ("менеджер геймы не починит квоту Google API").

async def capability_holders(cap: str, *, city: str | None = None) -> list[int]:
    """Every telegram_id currently entitled to `cap`, order-preserving de-duped (D-08: a
    person holding two roles that both grant `cap` is listed once). Bootstrap admins (D-12)
    always come first -- they hold every capability regardless of `staff`/registry state --
    followed by staff members of each enabled (D-10) role whose role_caps_* includes `cap`.
    Deliberately mirrors resolve_capabilities()'s own role-iteration shape rather than calling
    it per-candidate: resolve_capabilities would need every staff id up front to iterate over,
    which is exactly what this function is computing.

    Phase 09.2 (D, CITY-06): `city` is an OPTIONAL narrowing applied AFTER the list above is
    built -- it never changes who is a holder, only who among the holders is addressed this
    time. `city=None`, or the cities module being off, means "no filter", byte-identical to
    the pre-09.2 return value (regression contract). With a real `city`, `normalize_city` is
    applied to BOTH sides of the comparison (the requested city and each holder's
    `get_staff_city` binding) so a stray label/garbage binding still resolves to a known code
    the same way `admin_selected_city` does. A superadmin (`config.ADMIN_IDS`) is always kept
    regardless of binding (D-12); a holder with no binding at all (`get_staff_city` empty) is
    also always kept -- "all cities" is the pre-09.1 default for everyone (09.1 C).

    Two-layer fallback (RESEARCH Pitfall 4) -- both live INSIDE this primitive, not at a call
    site: (a) nobody holds `cap` at all -> `notify_by_capability` falls back to
    `config.ADMIN_IDS` (existing, below); (b) somebody holds `cap` but the city filter leaves
    NOBODY -> this function falls back to the unfiltered `holders` list, right here. Layer (b)
    must live here rather than in the caller, or `notify_by_capability`'s returned
    `sent_count` (the delegate-question UX branch depends on it) would stop reflecting reality
    the moment a filtered list goes empty."""
    holders: list[int] = []
    seen: set[int] = set()
    for admin_id in config.ADMIN_IDS:
        if admin_id not in seen:
            holders.append(admin_id)
            seen.add(admin_id)
    for role in ROLES:
        if await get_setting_typed(role_enabled_key(role)) != "on":
            continue  # D-10: role switched off entirely -> no holders from it
        role_caps = await get_setting_typed(role_caps_key(role)) or []
        if cap not in role_caps:
            continue
        for uid in await get_staff_ids_by_role(role):
            if uid not in seen:
                holders.append(uid)
                seen.add(uid)
    if city is None or not await cities_module_on():
        return holders
    target = normalize_city(city)
    filtered: list[int] = []
    for uid in holders:
        if uid in config.ADMIN_IDS:  # D-12: superadmins never filtered out
            filtered.append(uid)
            continue
        bound = await get_staff_city(uid)
        if not bound or normalize_city(bound) == target:
            filtered.append(uid)
    if not filtered:
        return holders  # layer (b): city filter emptied the list -> never drop the message
    return filtered


async def notify_by_capability(
    bot, cap: str, text: str, *, parse_mode: str | None = None, city: str | None = None
) -> int:
    """D-13 fan-out primitive: send `text` to every current holder of `cap`. T-08-31/D-13's
    single most important property -- a notification must NEVER be silently dropped -- if
    `capability_holders(cap)` comes back empty (nobody holds the capability, e.g. every
    reg_manager role is disabled), fall back to `config.ADMIN_IDS`. Per-recipient try/except
    (fail-soft) matches the shape every existing fan-out site already used, so one broken
    chat_id never blocks delivery to the rest. Returns the count of successful sends -- the
    delegate-question call site needs this to pick its reply text.

    Phase 09.2 (D, CITY-06): `city` is passed straight through to `capability_holders` -- see
    its docstring for the narrowing + fallback contract. Default `None` keeps every existing
    call site (payment.py, and any other caller not yet updated) byte-identical."""
    recipients = await capability_holders(cap, city=city)
    if not recipients:
        recipients = list(config.ADMIN_IDS)
    sent = 0
    for uid in recipients:
        try:
            await bot.send_message(uid, text, parse_mode=parse_mode)
            sent += 1
        except Exception as e:
            logger.error("notify_by_capability: failed to notify %s (cap=%s): %s", uid, cap, e)
    return sent


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
    # ── "*" (any capability at all) -- navigational entry points ───────────────────────────
    "admin_menu": ANY_CAPABILITY,
    "cmd:admin": ANY_CAPABILITY,
    # admin_city_switch/admin_city_pick:* (Phase 07.2, CITY-02) used to require the closest
    # single moderation capability, back when the header only scoped the moderation queues.
    # Phase 09.3 (CITY-08) makes the header the SOLE context for data screens AND settings AND
    # menu buttons -- a manager holding only "settings" (no moderate_reg) must still be able to
    # pick a city, or 09.3's own premise ("шапка задаёт контекст всему") breaks for them. This is
    # a navigational entry point, not a write right: the actual write is re-checked downstream
    # (`set_admin_city` refuses a bound manager outright, and every settings/menu write handler
    # re-checks `_per_city_visible_codes`) -- widening this to ANY_CAPABILITY does not widen who
    # can actually change anything.
    "admin_city_pick:*": ANY_CAPABILITY,
    "admin_city_switch": ANY_CAPABILITY,

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
    "admin_applications": "moderate_reg",
    "appr_all": "moderate_reg",
    "appr_all_no": "moderate_reg",
    # appr_all_yes (CR-02) is matched via F.data.startswith("appr_all_yes"), not "==" -- the
    # button carries an optional ":<city>" suffix. Prefix key, not the bare exact string.
    "appr_all_yes*": "moderate_reg",
    "appr_approve:*": "moderate_reg",
    "appr_reject:*": "moderate_reg",
    "appr_resume:*": "moderate_reg",
    "appr_skip:*": "moderate_reg",
    "admin_stuck_questions": "moderate_reg",  # T-08-33 quick task, part D: stuck-question list
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
    # Phase 14 (14-07, CITY-07): full CRUD screen — add/rename/tab-base/default/delete,
    # registered in the SAME commit as the handlers (09-01 convention). "city_del:*" does NOT
    # cover "city_del_go:*" — same prefix-divergence precedent as "gtdelete:*"/"gtdelete_go:*"
    # a few blocks up: both keys are required, not just the shorter one.
    "city_add": "settings",
    "city_rename:*": "settings",
    "city_tab:*": "settings",
    "city_default:*": "settings",
    "city_del:*": "settings",
    "city_del_go:*": "settings",
    "state:CityForm:*": "settings",
    "admin_consent_pdfs": "settings",
    "admin_dedupe_sheet": "settings",
    "admin_dedupe_sheet_go": "settings",
    "admin_event_preset": "settings",
    "admin_menu_buttons": "settings",
    "admin_reg_prompts": "settings",
    "admin_reg_questions": "settings",
    "admin_rebuild_sheet": "settings",
    "admin_rebuild_sheet_go": "settings",
    "admin_roles": "settings",
    # Экран прав роли с чекбоксами (quick 260813) — «roles_cap:*» не перехватывает
    # «roles_caps:...»: префикс различается символом на 10-й позиции.
    "roles_cap:*": "settings",
    "roles_caps:*": "settings",
    # Phase 07.3 (02, RET-01): «🔄 Новый сезон» wizard. `settings` here is necessary but NOT
    # sufficient — the real gate is `callback.from_user.id in config.ADMIN_IDS`, re-checked
    # inside every handler (season_reset_start/season_reset_go/season_reset_passphrase_step),
    # same posture as roles_city_start/roles_city_pick above. Registered interface-first, in
    # the SAME commit as the handlers (09-01 convention).
    "admin_season_reset": "settings",
    "season_reset_go": "settings",
    "state:SeasonReset:*": "settings",
    # Phase 07.3 (06, RET-04): «📥 Импорт прошлого события» wizard. Available to any `settings`
    # holder (CONTEXT D — unlike «Новый сезон», not superadmin-only): the action is additive,
    # existing records are never changed, and it's logged with the admin's id.
    "admin_season_import": "settings",
    "season_import_go": "settings",
    "state:SeasonImport:*": "settings",
    "admin_settings": "settings",
    "admin_settings_guide": "settings",
    "admin_sync_sheet": "settings",
    "city_toggle:*": "settings",
    "cmd:refresh_allowlist": "settings",
    "cmd:settings_guide": "settings",
    "consent_pdf_set:*": "settings",
    "menu_back": "settings",
    # Phase 09.3 (07, CITY-09): header-scoped «🔘 Кнопки главного меню» — «↩️ Все как везде»
    # replaces 09.2-06's five-entry per-city picker family (deleted from this dict entirely,
    # same closure shape as settings_edit_city*/settings_reset_city* above); same right as
    # every other menu_* screen, never a separate capability. Exact-key resolution
    # (required_capability checks `callback_data in ADMIN_CAPS` before any prefix scan) means
    # "menu_reset_city" is never swallowed by the "menu_reset_city_go:*" prefix below it.
    "menu_reset_city": "settings",
    "menu_reset_city_go:*": "settings",
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
    # Phase 09.1 (C, ROLE-03): manager <-> city binding. "roles_city:*" does not swallow
    # "roles_city_pick:*" -- the prefixes diverge at the char right after "roles_city"
    # (":" vs "_"), same shape already documented for "roles_cap:*"/"roles_caps:*" above.
    "roles_city:*": "settings",
    "roles_city_pick:*": "settings",
    "roles_toggle:*": "settings",
    "settings_back": "settings",
    "settings_cancel": "settings",
    "settings_edit:*": "settings",
    # Phase 09.3 (06, CITY-09): header-scoped per-key editor — «✏️ Изменить для {город}»/
    # «↩️ Как везде» replace 09.2-05's four-entry per-city picker family (deleted from this
    # dict); same right as every other settings_* screen, never a separate capability.
    "settings_edit_city:*": "settings",
    "settings_reset_city:*": "settings",
    "settings_reset_city_go:*": "settings",
    "settings_file:*": "settings",
    "settings_group:*": "settings",
    "settings_group_noop": "settings",
    "settings_photo:*": "settings",
    # Phase 09.3 (04, CITY-09): header-scoped registration-mode reset — same right as every
    # other settings_* screen, never a separate capability. "settings_regmode_reset" does not
    # swallow "settings_regmode_reset_go:*" (exact-match vs prefix, same shape already
    # documented for "roles_cap:*"/"roles_caps:*" above).
    "settings_regmode_reset": "settings",
    "settings_regmode_reset_go:*": "settings",
    # Quick 260815-3hw (Task 3): confirm-gate on overwriting an existing Google Sheets tab.
    "sheets_tab_confirm": "settings",
    "sheets_tab_cancel": "settings",
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

    # ── moderate_game (Phase 9) ─────────────────────────────────────────────────────────
    # 09-01 (interface-first): all 15 future gamification callback/state keys registered
    # BEFORE any handler exists, so waves 2-6 (09-02..09-06) never touch this file again --
    # removes the file-conflict risk between the parallel wave-2 plans (09-02 admin /
    # 09-03 delegate). One capability for all of it (D-domain): @osesska holds exactly
    # moderate_game, no finer split needed.
    "admin_game_tasks": "moderate_game",
    "gtnew": "moderate_game",
    "gtcat:*": "moderate_game",
    "gtproof:*": "moderate_game",
    # Phase 09.1 (A): «Готово» on the proof-type checkbox step -- new callback, not part of
    # 09-01's original 15-key interface-first set (the checkbox step itself is new).
    "gtproof_done": "moderate_game",
    # Phase 09.1 (B): "Кому задание?" city step -- new callback, gated behind cities_module_on.
    "gttcity:*": "moderate_game",
    "gtconfirm": "moderate_game",
    "gtcancel": "moderate_game",
    "state:GameTaskCreate:*": "moderate_game",
    "admin_game_review": "moderate_game",
    "grev_approve:*": "moderate_game",
    "grev_approve_custom:*": "moderate_game",
    "grev_reject:*": "moderate_game",
    "grev_skip:*": "moderate_game",
    "state:GameReview:*": "moderate_game",
    "admin_game_sync_sheet": "moderate_game",
    "admin_game_sync_sheet_go": "moderate_game",
    "admin_game_stats": "moderate_game",
    # Phase 14 (14-03, GAME-08): task archive/delete screen — registered in the SAME commit
    # as the handlers (09-01 convention). WARNING: "gtdelete:*" does NOT cover
    # "gtdelete_go:*" (prefix match is on "gtdelete:", the char right after "gtdelete" in
    # "gtdelete_go" is "_", not ":") — same precedent as roles_cap:*/roles_caps:*, both keys
    # are required, not just the shorter one.
    "admin_game_archive": "moderate_game",
    "gtarchive:*": "moderate_game",
    "gtarchive_go:*": "moderate_game",
    "gtunarchive:*": "moderate_game",
    "gtdelete:*": "moderate_game",
    "gtdelete_go:*": "moderate_game",

    # Quick 260819-gtl (title + cover photo): wizard's new photo-skip step, plus the
    # point-edit («✏️ Правка») screen on an existing task -- title/photo replace/remove.
    "gtphoto_skip": "moderate_game",
    "gtedit:*": "moderate_game",
    "gtedittitle:*": "moderate_game",
    "gteditphoto:*": "moderate_game",
    "gtremovephoto:*": "moderate_game",
    "state:GameTaskEdit:*": "moderate_game",
    # Phase 16 (16-03, GAME-UI-03): the rest of the point-edit card (description / coins /
    # deadline + «👁 Как видит делегат»), the wizard's deadline presets and its final-step
    # «✏️ Изменить» field menu. NOTE the same prefix gotcha as gtdelete:*/gtdelete_go:* --
    # "gteditdeadline:*" does NOT cover "gteditdeadline_preset:*", both are listed.
    "gteditdesc:*": "moderate_game",
    "gteditcoins:*": "moderate_game",
    "gteditdeadline:*": "moderate_game",
    "gteditdeadline_preset:*": "moderate_game",
    "gteditdeadline_custom": "moderate_game",
    "gtdeadline_preset:*": "moderate_game",
    "gtdeadline_custom": "moderate_game",
    "gtpreview:*": "moderate_game",
    "gtpreview_close": "moderate_game",
    "gtwiz_edit_menu": "moderate_game",
    "gtwiz_edit:*": "moderate_game",
    "gtwiz_back": "moderate_game",

    # Phase 14 (14-04, GAME-09): monetary right, not registration-queue right -- coins are
    # geyma's territory, not the applications queue's. Physically relocated out of the
    # applications-moderation block above (this is the single "cmd:coins" entry, not a dupe).
    "cmd:coins": "moderate_game",
    "admin_coins_manual": "moderate_game",
    "coinsman_sign:*": "moderate_game",
    "coinsman_confirm": "moderate_game",
    "coinsman_cancel": "moderate_game",
    "state:CoinsManual:*": "moderate_game",

    # Phase 14 (14-05, GAME-09): «📜 Журнал монет» — paginated read screen + CSV export,
    # registered in the SAME commit as the handlers (09-01 convention).
    "admin_coins_journal": "moderate_game",
    "coinsjrn_page:*": "moderate_game",
    "coinsjrn_csv": "moderate_game",

    # checkin: no keys yet -- Phase 12. Capability already exists in
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


# ── ROLE-01 (D-01): CapabilityMiddleware — the one enforcement point ────────────────────────
#
# T-08-14 (D-04): the exact user-facing copy every one of the (still-live, pre-08-04) 74
# inline `config.ADMIN_IDS` checks already shows — kept byte-identical so the toast text does
# not change from the user's point of view when this middleware takes over.
DENIAL_TEXT = "Недостаточно прав"

# UAT 17.08 (quick-260817-hvy, Task 1): human-facing copy for the emergency wizard-close path
# below — a manager whose capability got revoked mid-wizard is told exactly what to do next.
WIZARD_REVOKED_TEXT = (
    "Доступ к этому разделу отозван — мастер закрыт. Отправьте /admin, чтобы открыть меню."
)


def _is_question_reply_shape(message: Message) -> bool:
    """Same reply-shape predicate as handlers.admin.is_question_reply (🆔 + ❓ markers on the
    replied-to text) -- MINUS that function's own `from_user.id not in config.ADMIN_IDS` gate,
    which is the OLD access-control mechanism this middleware replaces. The capability decision
    itself lives entirely in CapabilityMiddleware.__call__ below."""
    replied = getattr(message, "reply_to_message", None)
    if not replied or not getattr(replied, "text", None):
        return False
    return "🆔" in replied.text and "❓" in replied.text


def _is_callback_shaped(event) -> bool:
    """Duck-typed, not `isinstance(event, CallbackQuery)`: aiogram's own `CallbackQuery` model
    defines a `data` field and no `text` field (verified: `"data" in CallbackQuery.model_fields`
    is True, `"text" in CallbackQuery.model_fields` is False; `Message` is the exact mirror).
    Real Telegram objects satisfy this exactly like `isinstance` would -- the reason to prefer
    it over `isinstance` is that this project's own dispatch-test harness
    (`tests/test_roles_phase8.py`, reused from 08-01/08-02) drives plain duck-typed
    `FakeCallback`/`FakeMessage` doubles through this exact middleware (Pitfall 4: it's the
    only place in the suite that exercises real `Router.propagate_event`), and those doubles
    are not `aiogram.types` subclasses -- matching this codebase's established Fake-object
    convention (CONVENTIONS.md), which every existing admin handler already relies on via
    plain attribute access, never `isinstance`."""
    return hasattr(event, "data") and not hasattr(event, "text")


def _holds(user_caps: set, cap: str) -> bool:
    """ANY_CAPABILITY means 'any non-empty set'; anything else is exact membership."""
    return bool(user_caps) if cap == ANY_CAPABILITY else cap in user_caps


def _required_caps_for_message(text: str | None, raw_state: str | None) -> list[str | None]:
    """Every capability that could gate a text message — because a slash-command typed while
    an FSM wizard is active can be dispatched to EITHER the top-level command handler OR the
    wizard-state handler depending on aiogram's registration order (first match wins, and the
    command handlers carry no StateFilter). Returning BOTH requirements and demanding the user
    clear all of them is fail-safe: it never grants more than the narrowest correct policy for
    whichever handler actually runs. This closes the escalation where a manager holding only
    the wizard's capability (e.g. moderate_game inside GameTaskCreate) types a privileged
    command (/export, /coins) and the middleware waved it through on the STATE's capability
    while aiogram ran the COMMAND handler.

    Non-command wizard text is gated by the state alone (unchanged); a question-reply-shaped
    message outside any wizard by the reply predicate (unchanged)."""
    command = _extract_command(text)
    if command is not None:
        caps = [required_capability(command=command)]
        if raw_state:
            caps.append(required_capability(raw_state=raw_state))
        return caps
    if raw_state:
        return [required_capability(raw_state=raw_state)]
    return [None]


async def _deny(event: TelegramObject, user_id: int, user_caps: set, cap: str | None):
    """D-04: reaction depends on who pressed. A known staff/admin member denied THIS specific
    capability (tapped a stale button, or a genuinely unmapped key -- T-08-12/D-02) gets the
    existing toast; a random user is met with silence (log only) so the admin surface's shape
    is never revealed (T-08-14). Returning `None` here absorbs the event (D-04's "known staff"
    branch is a final decision); returning `UNHANDLED` for the "no capabilities at all" branch
    lets the event keep propagating to its real router (T-08-16) -- see 08-03-PLAN.md's own
    note on why this distinction matters once 08-04 removes the `is_admin` filter."""
    if user_caps:
        if _is_callback_shaped(event):
            await event.answer(DENIAL_TEXT, show_alert=True)
        else:
            await event.answer(f"{DENIAL_TEXT}.")
        return None
    logger.info("CapabilityMiddleware: denied uid=%s cap=%s (no capabilities held)", user_id, cap)
    return UNHANDLED


async def _close_stale_wizard(data: dict) -> bool:
    """Сбрасывает FSM-стейт мастера, право на который у человека забрали. True — стейт
    действительно сброшен (значит, можно честно сказать «мастер закрыт»); False —
    FSMContext в data не пришёл или сброс не удался, тогда вызывающий обязан свалиться
    в обычный _deny и ничего не обещать."""
    fsm = data.get("state")
    if fsm is None:
        return False
    try:
        await fsm.clear()
        return True
    except Exception as e:
        logger.warning("CapabilityMiddleware: failed to clear stale wizard state: %s", e)
        return False


class CapabilityMiddleware(BaseMiddleware):
    """Inner middleware attached to `admin.router`'s own observers (D-01). MUST be registered
    via `.middleware()` -- deliberately NOT the router's outer-hook variant -- inner middleware
    only wraps the call of a HandlerObject whose own filter already matched (08-RESEARCH.md
    Pitfall 1, verified against the installed aiogram 3.24.0 source), so this class never runs
    for an event that belongs to a sibling router (payment/registration/user_actions)."""

    async def __call__(self, handler, event: TelegramObject, data: dict):
        user = data.get("event_from_user")
        if user is None:
            # No identity at all -- can't be a known staff/admin member either way; fail closed
            # exactly like the "stranger" branch of _deny (silent, event still propagates).
            return UNHANDLED

        raw_state = None
        if _is_callback_shaped(event):
            required = [required_capability(callback_data=event.data)]
            # Callback's raw_state stays None deliberately -- the emergency wizard-close path
            # below is intentionally not extended to callbacks (see comment there).
        elif hasattr(event, "text"):
            # A slash-command typed mid-wizard can dispatch to either the command handler or the
            # state handler (registration order decides) -- so _required_caps_for_message returns
            # BOTH requirements and the user must clear all of them (fail-safe against the
            # state-beats-command escalation). Non-command wizard text -> state only.
            raw_state = data.get("raw_state")
            required = _required_caps_for_message(event.text, raw_state)
            # Question-reply predicate applies only outside any wizard, to non-command text --
            # preserved from the original resolution order.
            if not raw_state and _extract_command(event.text) is None:
                required = [required_capability(special="question_reply")
                            if _is_question_reply_shape(event) else None]
        else:
            required = [None]

        user_caps = await resolve_capabilities(user.id)  # D-05: fresh SQLite read every call

        # UAT 17.08: право на мастер отозвали, пока человек был внутри мастера. Тогда
        # _required_caps_for_message требует и cap команды, И cap стейта -> заперты даже /admin
        # и /cancel, выхода нет до рестарта бота. Стейт здесь уже мёртвый: держателем этого
        # права человек не является, ни один шаг мастера ему всё равно не отработает. Закрываем
        # мастер и объясняем, что делать. НИЧЕГО не пропускаем: событие всё равно не доходит до
        # обработчика, поэтому эскалация «команда исполняется на праве стейта» (аудит 260816)
        # не открывается — на следующем сообщении raw_state уже пуст и работают обычные правила.
        if raw_state is not None:
            state_cap = required_capability(raw_state=raw_state)
            if state_cap is not None and not _holds(user_caps, state_cap):
                if await _close_stale_wizard(data):
                    if user_caps:
                        await event.answer(WIZARD_REVOKED_TEXT)
                        return None
                    logger.info(
                        "CapabilityMiddleware: cleared stale wizard state=%s for uid=%s (no capabilities held)",
                        raw_state, user.id,
                    )
                    return UNHANDLED

        # Deny-by-default (D-02): a message/callback whose ONLY signal is an unmapped key (every
        # entry None) is denied. Otherwise the user must satisfy EVERY applicable capability.
        applicable = [c for c in required if c is not None]
        if applicable and all(_holds(user_caps, c) for c in applicable):
            return await handler(event, data)

        return await _deny(event, user.id, user_caps, applicable[0] if applicable else None)
