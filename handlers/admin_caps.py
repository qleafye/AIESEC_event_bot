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
from database.db import get_staff_roles, get_staff_ids_by_role
from settings_schema import get_setting_typed

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

async def capability_holders(cap: str) -> list[int]:
    """Every telegram_id currently entitled to `cap`, order-preserving de-duped (D-08: a
    person holding two roles that both grant `cap` is listed once). Bootstrap admins (D-12)
    always come first -- they hold every capability regardless of `staff`/registry state --
    followed by staff members of each enabled (D-10) role whose role_caps_* includes `cap`.
    Deliberately mirrors resolve_capabilities()'s own role-iteration shape rather than calling
    it per-candidate: resolve_capabilities would need every staff id up front to iterate over,
    which is exactly what this function is computing."""
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
    return holders


async def notify_by_capability(bot, cap: str, text: str, *, parse_mode: str | None = None) -> int:
    """D-13 fan-out primitive: send `text` to every current holder of `cap`. T-08-31/D-13's
    single most important property -- a notification must NEVER be silently dropped -- if
    `capability_holders(cap)` comes back empty (nobody holds the capability, e.g. every
    reg_manager role is disabled), fall back to `config.ADMIN_IDS`. Per-recipient try/except
    (fail-soft) matches the shape every existing fan-out site already used, so one broken
    chat_id never blocks delivery to the rest. Returns the count of successful sends -- the
    delegate-question call site needs this to pick its reply text."""
    recipients = await capability_holders(cap)
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
    "admin_stuck_questions": "moderate_reg",  # T-08-33 quick task, part D: stuck-question list
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
    "admin_rebuild_sheet_go": "settings",
    "admin_roles": "settings",
    # Экран прав роли с чекбоксами (quick 260813) — «roles_cap:*» не перехватывает
    # «roles_caps:...»: префикс различается символом на 10-й позиции.
    "roles_cap:*": "settings",
    "roles_caps:*": "settings",
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


# ── ROLE-01 (D-01): CapabilityMiddleware — the one enforcement point ────────────────────────
#
# T-08-14 (D-04): the exact user-facing copy every one of the (still-live, pre-08-04) 74
# inline `config.ADMIN_IDS` checks already shows — kept byte-identical so the toast text does
# not change from the user's point of view when this middleware takes over.
DENIAL_TEXT = "Недостаточно прав"


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

        if _is_callback_shaped(event):
            cap = required_capability(callback_data=event.data)
        elif hasattr(event, "text"):
            # T-08-12 Pitfall 3 order: state (mid-wizard continuation) beats command beats the
            # bespoke question-reply predicate -- mirrors required_capability()'s own priority
            # and the /cancel-mid-Broadcast-wizard case documented in the capability_map.
            raw_state = data.get("raw_state")
            if raw_state:
                cap = required_capability(raw_state=raw_state)
            else:
                command = _extract_command(event.text)
                if command is not None:
                    cap = required_capability(command=command)
                elif _is_question_reply_shape(event):
                    cap = required_capability(special="question_reply")
                else:
                    cap = None
        else:
            cap = None

        user_caps = await resolve_capabilities(user.id)  # D-05: fresh SQLite read every call

        if cap is not None:
            allowed = bool(user_caps) if cap == ANY_CAPABILITY else cap in user_caps
            if allowed:
                return await handler(event, data)

        return await _deny(event, user.id, user_caps, cap)
