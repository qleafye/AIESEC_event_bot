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
