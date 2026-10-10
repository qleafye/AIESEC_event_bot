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
import time

from aiogram import BaseMiddleware
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.exceptions import TelegramForbiddenError
from aiogram.types import CallbackQuery, Message, TelegramObject

from config import config
from database.db import get_staff_roles, get_staff_ids_by_role, get_staff_city, get_user
from domain.settings.schema import get_setting_typed
# Phase 09.2 (D): city filter for capability_holders/notify_by_capability. domain/cities.py imports
# only config/database.db/settings_schema (see domain/cities.py's own module docstring) -- it never
# imports handlers.*, so importing it here from handlers/admin_caps.py cannot form a cycle.
from domain.cities import cities_module_on, normalize_city
from services import staff_reach  # 29.09: отметка «уведомления не доходят», fail-soft

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
    # D-41 (ревью 28.09): одобрять человека у стойки — отдельно от отметки входа. Волонтёру
    # у двери зала (D-42) хватает `checkin`; решение о пропуске неодобренного — у DXP/DXR и
    # волонтёров регистрации (роль reg_volunteer ниже) и у держателей moderate_reg.
    "checkin_approve",
    # Роль «📣 Маркетинг (метки)»: ссылки с метками и их статистика — и больше ничего. Право
    # само по себе ни одного экрана с данными делегатов не открывает (см. ADMIN_CAPS ниже).
    "source_links",
]

CAP_LABELS = {
    "moderate_reg": "📋 Модерация заявок",
    "moderate_receipts": "🧾 Модерация чеков",
    "moderate_game": "🎮 Модерация геймификации",
    "broadcast": "📢 Рассылки",
    "settings": "⚙️ Настройки",
    "stats": "📊 Статистика",
    "checkin": "✅ Отметки на форуме (чек-ин)",
    "checkin_approve": "📝 Одобрение на месте",
    "source_links": "🔗 Ссылки с метками",
}

# D-07: roles fixed in code today (admin / reg_manager / game_manager), but the SHAPE is
# data-driven — a fourth role costs exactly one entry here plus two SETTINGS_SCHEMA keys
# (role_caps_<role>/role_<role>_enabled), no refactor. D-12: "admin" deliberately has NO
# entry here and NO registry keys — admin access is config.ADMIN_IDS, un-revocable from the
# bot; see resolve_capabilities()'s bootstrap short-circuit below.
# Quick 260919 (ln7): stats_manager is exactly that promised next entry — one row here, two
# SETTINGS_SCHEMA keys, nothing else.
ROLES = {
    "reg_manager": {
        "label": "🛂 Менеджер регистраций",
        "default_caps": ["moderate_reg", "moderate_receipts"],
    },
    "game_manager": {
        "label": "🎮 Менеджер геймификации",
        "default_caps": ["moderate_game"],
    },
    "stats_manager": {
        "label": "📊 Менеджер статистики",
        "default_caps": ["stats"],
    },
    # Идея №5 бэклога чек-ина (`.planning/IDEAS-CHECKIN-BACKLOG-260924.md`): волонтёр,
    # приглашённый ссылкой (`handlers/admin_volunteer_invite.py`) или добавленный вручную тем
    # же общим экраном «👥 Роли и доступы» — держит РОВНО право `checkin`, ничего больше.
    # Заведена как обычная запись ROLES (не спецказус) — ссылка-приглашение зовёт тот же
    # `database.db.add_staff(role="volunteer")`, что и ручная выдача.
    "volunteer": {
        "label": "🎗 Волонтёр форума",
        "default_caps": ["checkin"],
    },
    # D-41 (ревью 28.09): волонтёр стойки регистрации — отметка входа И одобрение на месте.
    # Выдаётся ссылкой-приглашением с опцией «с одобрением на месте» или вручную.
    "reg_volunteer": {
        "label": "🎗 Волонтёр регистрации",
        "default_caps": ["checkin", "checkin_approve"],
    },
    # Маркетолог (запрос РилТолка): ставит свои метки на ссылки и смотрит, сколько заявок
    # пришло по каждой, — без заявок, контактов и настроек.
    "marketing_manager": {
        "label": "📣 Маркетинг (метки)",
        "default_caps": ["source_links"],
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
# Four sites (new application/registration.py, new receipt/payment.py, delegate question/
# user_actions.py, pending-applications reminder/services.reminders.py) route to whoever HOLDS
# the relevant capability, not the bare ADMIN_IDS list -- a reg_manager who isn't in
# config.ADMIN_IDS must still see new/pending applications. Two technical-failure sites
# (services/sheets.py, services/scheduler.py) deliberately keep the old ADMIN_IDS loop -- D-13
# explicitly does NOT route those to holders ("менеджер геймы не починит квоту Google API").

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


# Модератор заблокировал бота — сообщаем живым админам не чаще раза в сутки на человека;
# словарь процессный (не в БД), после рестарта алерт повторится — это приемлемо.
_blocked_notified_at: dict[int, float] = {}
_BLOCKED_ALERT_COOLDOWN = 24 * 60 * 60


async def _blocked_moderator_alert_text(uid: int) -> str:
    user = await get_user(uid)
    raw_username = (user or {}).get("username")
    uname = (raw_username or "").lstrip("@")
    who = f"{uid} (@{uname})" if uname and uname != "-" else str(uid)
    return (
        f"⚠️ Модератор {who} заблокировал бота — уведомления о заявках ему не доходят.\n"
        "Чтобы вернуть: пусть откроет бота и нажмёт /start."
    )


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
    call site (payment.py, and any other caller not yet updated) byte-identical.

    A moderator who blocked the bot (`TelegramForbiddenError`) is never counted in `sent` and
    never keeps retrying silently into the error log on every application -- the first
    Forbidden for a given uid logs one WARNING and queues one human-readable alert to
    `config.ADMIN_IDS` (minus the blocked uid and minus anyone else who also failed with
    Forbidden in this same call); repeats within 24h are `logger.debug` only. The alert send
    itself is fail-soft and never affects the returned `sent` count."""
    recipients = await capability_holders(cap, city=city)
    if not recipients:
        recipients = list(config.ADMIN_IDS)
    sent = 0
    blocked: list[int] = []
    to_alert: list[int] = []
    now = time.time()
    for uid in recipients:
        try:
            await bot.send_message(uid, text, parse_mode=parse_mode)
            sent += 1
            await staff_reach.note_delivered(uid)
        except TelegramForbiddenError as e:
            blocked.append(uid)
            await staff_reach.note_undeliverable(uid, e)
            if now - _blocked_notified_at.get(uid, 0) >= _BLOCKED_ALERT_COOLDOWN:
                to_alert.append(uid)
                _blocked_notified_at[uid] = now
            else:
                logger.debug(
                    "notify_by_capability: %s still has the bot blocked (cap=%s), alert on cooldown",
                    uid, cap,
                )
        except Exception as e:
            if not staff_reach.is_unreachable_error(e):
                logger.error("notify_by_capability: failed to notify %s (cap=%s): %s", uid, cap, e)
                continue
            await staff_reach.note_undeliverable(uid, e)  # «chat not found»: не нажимал /start
            log = logger.warning if staff_reach.first_warning_today(uid) else logger.debug
            log("notify_by_capability: %s is unreachable (cap=%s): %s", uid, cap, e)

    if to_alert:
        try:
            for uid in to_alert:
                logger.warning(
                    "notify_by_capability: moderator %s blocked the bot (cap=%s) -- alerting admins",
                    uid, cap,
                )
                alert_text = await _blocked_moderator_alert_text(uid)
                alert_recipients = [a for a in config.ADMIN_IDS if a != uid and a not in blocked]
                for admin_id in alert_recipients:
                    try:
                        await bot.send_message(admin_id, alert_text)
                    except Exception as alert_e:
                        logger.warning(
                            "notify_by_capability: failed to alert %s about blocked moderator %s: %s",
                            admin_id, uid, alert_e,
                        )
        except Exception as e:
            logger.warning("notify_by_capability: blocked-moderator alert block failed: %s", e)

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
# Value tuple ("stats", "source_links") means «любое из»: ключ открыт держателю ХОТЯ БЫ ОДНОГО
# из прав. Проверяет `_holds` — единственное место, где значение карты сравнивается с правами.
#
# T-08-12 (deny-by-default, D-02): a callback/command with no entry here resolves to None in
# required_capability(), which the middleware treats as an outright deny -- a new button is
# broken-by-default until someone adds its key, never silently open to everyone.
ANY_CAPABILITY = "*"

ADMIN_CAPS: dict[str, str | tuple[str, ...]] = {
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
    # Ревью фазы 20: переключатель получил форму «admin_city_switch:{раздел}» (несёт экран, с
    # которого его открыли), поэтому ключ стал префиксным — право то же самое, ANY_CAPABILITY,
    # и покрывает обе формы: голую (корень, стейл-кнопки) и с разделом.
    "admin_city_pick:*": ANY_CAPABILITY,
    "admin_city_switch*": ANY_CAPABILITY,
    # Phase 20 (20-01, ADMIN-IA-01): admin_sec:{token} -- экран раздела админки. Это
    # навигационная точка входа, а не право: содержимое раздела повторно фильтруется
    # ПОСТРОЧНО этой же картой (handlers/admin_sections.py::visible_rows -> required_capability
    # + _holds), раздел без единой доступной строки не открывается вовсе, а каждый реальный
    # callback внутри по-прежнему проверяется CapabilityMiddleware независимо. Тот же приём и
    # то же обоснование, что у admin_city_switch выше (09.3): открыть навигацию всем, кто
    # вообще имеет доступ в панель, не расширяет того, что человек может сделать.
    "admin_sec:*": ANY_CAPABILITY,

    # ── stats ────────────────────────────────────────────────────────────────────────────
    "admin_export_csv": "stats",
    # «📊 Данные»: «👥 Список участников» (CSV одобренных) — менеджеру регистраций и статистике;
    # «📄 Открыть таблицу» (url-кнопка, своего callback нет) — тем, кто синхронизирует таблицу,
    # плюс менеджеру регистраций: он и так читает в ней заявки.
    "admin_export_participants": ("moderate_reg", "stats"),
    "admin_open_sheet": ("settings", "moderate_reg"),
    "admin_export_incomplete": "stats",
    "admin_monthly_stats": "stats",
    # Источники видны и маркетологу: та же статистика «метка -> число заявок», без людей.
    "admin_source_stats": ("stats", "source_links"),
    "admin_stats": "stats",
    # «🔗 Ссылки с метками» (handlers/applications/admin_source_links.py): экран и мастер новой ссылки.
    "admin_source_links": "source_links",
    "srclink_new": "source_links",
    "srclink_cancel": "source_links",
    "state:SourceLinkCreate:*": "source_links",
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
    # Phase 21 (21-07, T-21-09): кнопка «🕓 История» правок анкеты в карточке заявки — та же
    # капа, что и вся остальная карточка, чужую историю без прав не открыть.
    "appr_history:*": "moderate_reg",
    # Quick 260902-tzh: «📄 Полная анкета» — та же капа, что вся остальная карточка заявки.
    "appr_full:*": "moderate_reg",
    "appr_reject:*": "moderate_reg",
    "appr_resume:*": "moderate_reg",
    "appr_skip:*": "moderate_reg",
    # Phase 31 (31-11, D-20): чип «только помеченные правилами» — та же капа, что весь
    # остальной appr_* (действие над очередью заявок, не настройка).
    "appr_flag:*": "moderate_reg",
    "admin_stuck_questions": "moderate_reg",  # T-08-33 quick task, part D: stuck-question list
    # Quick 260904-2cj (QJRN-01..04): раздел «❓ Вопросы делегатов» — экран, фильтр/страница,
    # ответ из экрана. `admin_stuck_questions` ОСТАЁТСЯ выше — callback жив как алиас на тот же
    # экран (клавиатуры, живущие в старых чатах), снятие записи закрыло бы его deny-by-default.
    "admin_questions": "moderate_reg",
    "aq:*": "moderate_reg",
    "aq_answer:*": "moderate_reg",
    "state:QuestionAnswer:*": "moderate_reg",
    # Квик 260914-rgq (RGQ-01): экран «📇 Список заявок» — тот же гейт, что у очереди заявок
    # (`admin_applications` выше): список показывает те же персональные данные делегатов.
    "admin_app_list": "moderate_reg",
    "apl:*": "moderate_reg",
    # Quick 260906-8uq (FAQ-01..06): раздел «❓ Частые вопросы» — тот же `moderate_reg`, что
    # журнал вопросов выше (FAQ ведут те же люди, что отвечают делегатам; иначе кнопка
    # «❓ В FAQ» из задачи 4 оказалась бы недоступна тому, кто только что ответил). Один
    # префиксный ключ на всё пространство callback'ов менеджерского экрана.
    "admin_faq": "moderate_reg",
    "afaq_*": "moderate_reg",
    "state:FaqItem:*": "moderate_reg",
    # Делегации вузов (handlers/delegations/admin_delegations.py, handlers/delegations/admin_delegations_review.py):
    # менеджер заявок ведёт делегации сам — выбор формы и вопросов, тумблер геймы, отсечка ЦА,
    # курсы, тексты, проверка курса и ручная привязка. Право то же, что у модерации заявок;
    # один префиксный ключ `dlg_*` на все callback'и экрана и его подэкранов.
    "admin_delegations": "moderate_reg",
    "dlg_*": "moderate_reg",
    "state:DelegationEdit:*": "moderate_reg",
    "state:DelegationLink:*": "moderate_reg",
    # Phase 27 (27-06, LANG-05/09): экран «🌐 Английские тексты» — тот же гейт, что у соседних
    # экранов раздела «📝 Анкета» (admin_reg_prompts и т.д.), не заводим нового.
    "admin_i18n": "settings",
    "admin_i18n:*": "settings",
    "admin_i18n_noop": "settings",
    "admin_i18n_row:*": "settings",
    "admin_i18n_edit:*": "settings",
    "admin_i18n_edit_new:*": "settings",
    "admin_i18n_retr:*": "settings",
    "admin_i18n_retr_go:*": "settings",
    # Квик 260912 (W5, Задача 4) — «догонялка перевода».
    "admin_i18n_seed": "settings",
    "state:AdminI18nEdit:*": "settings",
    # Фаза внешних форм (Яндекс/Google): раздел «📝 Внешние формы» — настройки, как у соседних
    # интеграций. «Ответы форм» на карточке /find (extf_view:*) — та же капа, что cmd:find;
    # префикс длиннее, поэтому выигрывает у extf_*.
    "admin_ext_forms": "settings",
    "extf_*": "settings",
    "extf_view:*": "moderate_reg",
    "state:ExtFormConnect:*": "settings",
    "state:ExtFormImport:*": "settings",
    "state:ExtFormOAuth:*": "settings",
    "state:ExtFormAppKeys:*": "settings",
    # Форум-ночь п.8 (идея №19, SOS): экран менеджера «🆘 SOS» — та же капа, что журнал
    # вопросов выше. «Беру»/«✅ Решено» на ЛИЧНОЙ копии карточки — `moderate_reg`; в чате SOS
    # их (и реплай) разбирает handlers/chat/group_chat.py — там жмёт любой участник чата SOS.
    "admin_sos": "moderate_reg",
    "asos:*": "moderate_reg",
    "asos_city:*": "moderate_reg",  # вход из «🎪 Форум: функции» с городом хаба
    "sos_claim:*": "moderate_reg",
    "sos_resolve:*": "moderate_reg",
    "sos_takeover:*": "moderate_reg",  # «🔁 Перехватить»: город заявки сверяет хендлер
    "sos_takeover_go:*": "moderate_reg",
    "sos_takeover_no:*": "moderate_reg",
    "special:sos_reply": "moderate_reg",
    # Привязка чата SOS — интеграционная настройка, та же капа, что у остальной привязки чата
    # (services/chat_tracking.py::is_bot_admin_user требует `settings`).
    "asos_bind": "settings",
    "asos_bind_cancel": "settings",
    "state:SosChatBind:*": "settings",
    # Ревью 24.09 (находка 1/3, аудит ключей после 8c0d8af): «⚙️ Тексты и тайминги» —
    # конфигурирование контента/таймингов SOS, та же капа, что привязка чата выше, не
    # `moderate_reg` (это не действие над конкретным обращением).
    "asos_settings": "settings",
    "asos_noop": "settings",
    "asos_set_delay:*": "settings",
    "asos_delay_custom:*": "settings",
    "asos_settings_edit:*": "settings",
    "cmd:create_link": ("moderate_reg", "source_links"),
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
    # Quick 260910-okb (BC-05): экран «🗒 Последние рассылки» — та же capability, что весь
    # раздел; строки списка несут доступ к отзыву (bc_rev*) ниже.
    "admin_broadcast_log": "broadcast",
    # UAT 15.09: кнопка «⏰ Запланированные» на экране рассылок — та же capability, что весь
    # раздел; открывает список, который раньше был виден только скрытой командой /scheduled.
    "admin_broadcast_scheduled": "broadcast",
    # Quick 260910-okb (BC-01..06): превью+подтверждение/прогресс/стоп немедленной рассылки и
    # отзыв у получателей — та же capability, что и весь остальной раздел «Рассылки».
    "bc_go": "broadcast",
    "bc_no": "broadcast",
    # Форум-ночь п.7 (D-XX, «❗ Важное»): тумблер важности на превью немедленной рассылки —
    # та же capability, что bc_go/bc_no рядом.
    "bc_important_toggle": "broadcast",
    "bc_rev:*": "broadcast",
    "bc_revgo:*": "broadcast",
    "bc_revno": "broadcast",
    "bc_stop:*": "broadcast",
    # Quick 260904-dq1: предупреждение о тихих часах на шаге планирования — та же capability,
    # что у соседних шагов рассылки.
    "bcast_quiet:*": "broadcast",
    "broadcast_all": "broadcast",
    "broadcast_cancel": "broadcast",
    "broadcast_filter": "broadcast",
    "broadcast_incomplete": "broadcast",
    "broadcast_local": "broadcast",
    "broadcast_schedule": "broadcast",
    "broadcast_unsubscribed": "broadcast",
    "cmd:broadcast": "broadcast",
    "cmd:broadcasts": "broadcast",
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
    # Форум-ночь п.6 (D-25, идея №14): мастер «Были/Не были на сессии …»
    # (handlers/comms/admin_broadcast_session_filter.py) — тот же мастер фильтра рассылки, та же
    # капа, что filter_f_*/filter_opt:* выше.
    "cksf_start:*": "broadcast",
    "cksf_city:*": "broadcast",
    "cksf_day:*": "broadcast",
    "cksf_pick:*": "broadcast",
    "cksf_cancel": "broadcast",
    # Фильтры рассылки «Записан на сессию / Не записался / Не прошёл тест» и сезон в рассылке.
    "enrf_*": "broadcast",
    "bcseason_*": "broadcast",
    "extff_*": "broadcast",
    "sched_cancel_*": "broadcast",
    # Форум-ночь п.7 (D-XX, «❗ Важное»): тумблер важности + подтверждение/отмена планирования
    # отложенной рассылки (handlers/comms/admin_broadcasts.py::sched_*) — та же capability, что и
    # весь мастер планирования (sched_cancel_* выше).
    "sched_important_toggle": "broadcast",
    "sched_go": "broadcast",
    "sched_no": "broadcast",
    "state:Broadcast:*": "broadcast",
    # «📊 Опросы» (handlers/comms/admin_polls.py + admin_poll_wizard.py) — то же право, что и
    # рассылки: опрос уходит той же аудитории тем же каналом. Без нового capability.
    "admin_polls": "broadcast",
    "admin_polls_closed": "broadcast",
    "poll_new": "broadcast",
    "poll_opts_done": "broadcast",
    "poll_tg_anon": "broadcast",
    "poll_tg_multi": "broadcast",
    "poll_settings_next": "broadcast",
    "poll_aud:*": "broadcast",
    "poll_send_now": "broadcast",
    "poll_schedule": "broadcast",
    "poll_cancel": "broadcast",
    "poll_card:*": "broadcast",
    "poll_close:*": "broadcast",
    "poll_export:*": "broadcast",
    "poll_del:*": "broadcast",
    "poll_del_go:*": "broadcast",
    "state:PollCreate:*": "broadcast",

    # ── settings ─────────────────────────────────────────────────────────────────────────
    "approval_auto_go:*": "settings",
    "approval_auto_no:*": "settings",
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
    # Экран «📊 Дашборд» (Phase 15, 15-02, D-19) — тумблеры блоков веб-дашборда.
    "admin_dashboard_settings": "settings",
    "dash_block:*": "settings",
    # Экран «🎨 Оформление» Mini App (Phase 19, 19-08, D-06) — тумблеры, чекбоксы разделов.
    # handlers/admin_miniapp.py.
    "admin_miniapp_settings": "settings",
    "miniapp_toggle_enabled": "settings",
    "miniapp_toggle_staff_only": "settings",
    "miniapp_section:*": "settings",
    # Второй шов «🎭 Пресеты и ручки оформления» (Phase 19.1, 07, D-20) —
    # handlers/admin_miniapp_theme.py. Правка акцента/лого (старые "miniapp_edit_accent"/
    # "miniapp_edit_logo"/"miniapp_remove_logo"/"miniapp_cancel_edit") заменена этим блоком —
    # правка акцента теперь идёт через "miniapp_theme_color:accent", лого — через
    # "miniapp_theme_photo:logo"/"miniapp_theme_remove_photo:logo".
    "miniapp_cycle_motion": "settings",  # квик 4mw: кнопка-цикл «✨ Анимации» на экране оформления
    "miniapp_theme_open": "settings",
    "miniapp_theme_noop": "settings",
    "miniapp_theme_cancel_edit": "settings",
    "miniapp_preset:*": "settings",
    "miniapp_preset_apply:*": "settings",
    "miniapp_preset_cancel": "settings",
    "miniapp_theme_reset": "settings",
    "miniapp_theme_reset_go": "settings",
    "miniapp_theme_color:*": "settings",
    "miniapp_theme_font:*": "settings",
    "miniapp_theme_toggle_playful": "settings",
    "miniapp_theme_toggle_pattern": "settings",
    "miniapp_theme_photo:*": "settings",
    "miniapp_theme_remove_photo:*": "settings",
    "state:MiniAppTheme:*": "settings",
    "admin_dedupe_sheet": "settings",
    "admin_dedupe_sheet_go": "settings",
    "admin_event_preset": "settings",
    "admin_menu_buttons": "settings",
    # 09.10: «🖼 Аватар бота» (handlers/admin_bot_avatar.py) — та же капа, что у раздела.
    "admin_bot_avatar": "settings",
    "botava_*": "settings",
    "state:BotAvatar:*": "settings",
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
    # «🔗 Какая таблица»: как «Новый сезон» — настоящий гейт ADMIN_IDS в handlers/sheets/admin_sheet_target.py.
    "admin_sheet_target": "settings", "sheet_target_*": "settings", "state:SheetTarget:*": "settings",
    # Phase 07.3 (06, RET-04): «📥 Импорт прошлого события» wizard. Available to any `settings`
    # holder (CONTEXT D — unlike «Новый сезон», not superadmin-only): the action is additive,
    # existing records are never changed, and it's logged with the admin's id.
    "admin_season_import": "settings",
    "season_import_go": "settings",
    "state:SeasonImport:*": "settings",
    "admin_settings": "settings",
    "toggle_game_submit_notify": "settings",  # Quick 260822: тумблер дайджеста сдач (экран 🎮)
    "admin_settings_guide": "settings",
    # Quick 260902-tzh: «🧾 Поля карточки заявки» — та же капа, что остальные экраны настроек.
    "modcard_open": "settings",
    "modcard_toggle:*": "settings",
    "modcard_limit:*": "settings",
    "modcard_sync": "settings",  # Квик 260919-m9x: «показать всё, что спрашиваем»
    "modcard_noop": "settings",
    # Phase 28 (28-08, SU-08): «🧮 Правила балла» — тот же класс экрана настроек, что
    # «🧾 Поля карточки заявки» выше (не moderate_reg — это конфигурирование правил, а не
    # действие над конкретной заявкой).
    "admin_reg_scoring": "settings",
    "scoring_toggle:*": "settings",
    "scoring_limit:*": "settings",
    "scoring_drop:*": "settings",
    "scoring_noop": "settings",
    # Phase 31 (31-08, D-09/D-15/D-16): экран «🚫 Правила автоотказа» — тот же класс экрана
    # настроек, что «🧮 Правила балла» выше (конфигурирование, не действие над заявкой). Один
    # префиксный ключ на всё пространство callback'ов редактора (arr_v/arr_t/arr_master/
    # arr_act/arr_city/arr_citypick/arr_track/arr_name/arr_text/arr_copy/arr_copygo/arr_new/
    # arr_preset/arr_p/arr_d/arr_dgo/arr_noop — все начинаются с "arr_").
    "admin_reject_rules": "settings",
    "arr_*": "settings",
    "state:RejectRuleEdit:*": "settings",
    # Phase 31 (31-10, D-01/D-13/D-16): конструктор условий (handlers/applications/admin_reject_cond.py) —
    # тот же класс экрана, тем же правом; один префиксный ключ на всё пространство callback'ов
    # (arc_add/arc_steppage/arc_step/arc_op/arc_val/arc_valpage/arc_valdone/arc_num/arc_del/
    # arc_dellist/arc_preset/arc_dry/arc_gate/arc_dry_go/arc_cancel — все начинаются с "arc_").
    "arc_*": "settings",
    "state:RejectCond:*": "settings",
    # Phase 31 (31-11, D-18/D-19): журнал «🤖 Автоотказы» — moderate_reg (работа модератора,
    # не настройщика), хотя вход виден с экрана правил под settings; экран журнала
    # перепроверяет своё право сам (не полагается на переход с чужого правом экрана).
    "admin_reject_journal": "moderate_reg",
    "arj_*": "moderate_reg",
    # Квик 260923 (AUTOREJ-REPORT, D-I): «📊 Отчётность автоотказа» — тот же класс экрана, та же
    # капа, что «🚫 Правила автоотказа» выше (конфигурирование, вход с того же экрана — разрыва
    # прав нет, в отличие от журнала строкой выше).
    "admin_reject_reports": "settings",
    "rjretro": "settings",
    "rjretro_p:*": "settings",
    "rjretro_go:*": "settings",
    "arp_*": "settings",
    # Форум-ночь п.4 (расписание форума в боте): раздел «🗓 Программа форума» — тот же класс
    # экрана настроек, что «🚫 Правила автоотказа»/«🧮 Правила балла» выше (конфигурирование
    # контента события, не действие над конкретной заявкой). Один префиксный ключ на всё
    # пространство callback'ов шва handlers/admin_program.py (prog_v/prog_day/prog_new/
    # prog_field/prog_hall*/prog_copy*/prog_d/prog_dgo/... — все начинаются с "prog_").
    "admin_program": "settings",
    "prog_*": "settings",
    "state:ProgramSessionField:*": "settings",
    "state:ProgramHallName:*": "settings",
    "state:ProgramDayCustom:*": "settings",
    # Запись на сессии и тест компетенций: треки, компетенции, лимит мест, правка и импорт теста.
    "state:ProgramTrackEdit:*": "settings",
    "state:ProgramCompetencyEdit:*": "settings",
    "state:ProgramEnrollLimit:*": "settings",
    "state:QuizEdit:*": "settings",
    "state:QuizImport:*": "settings",
    "state:ProgramPhotoUpload:*": "settings",
    # Quick 260911-805 (W4-03): «🌙 Тихие часы» — тот же класс экрана настроек, что «🧾 Поля
    # карточки заявки»/«🧮 Правила балла» выше (D-02: deny-by-default — без записи строка
    # раздела не рисуется вовсе); строка-вход требует `settings`, менеджер только с
    # `moderate_reg` продолжает видеть раздел «📋 Заявки» набором операций.
    "admin_quiet_hours": "settings",
    # Правка 15.09 (владелец, «привязка через личку админа»): экран «💬 Чат» снесён целиком —
    # привязка теперь идёт через личку (see handlers/chat/group_chat.py), без единого callback на
    # `admin.router`, а значит и без записи в этом реестре. Тумблер учёта переехал в общий
    # список тумблеров раздела «🔧 Управление» — та же капа, что у «🌙 Тихие часы» выше.
    "toggle_chat_tracking_enabled": "settings",
    "admin_chat_rating": "settings",  # Квик 260927: экран «🏆 Рейтинг чата» (раздел «🔧 Управление»)
    "chimp:*": "settings",  # «📥 Загрузить историю чата» с экрана рейтинга: выбор чата, подтверждение
    "state:ChatExportImport:*": "settings",  # ожидание файла result.json и подтверждение
    "chrate:*": "settings",  # его кнопки: режим, суммы правил, галочки заданий
    "state:ChatRatingEdit:*": "settings",  # ввод суммы правила / названия баллов
    "admin_chat_cleanup": "settings",  # Квик 260927: экран «🧹 Служебные сообщения в чате»
    "admin_chat_reconcile": "settings",  # 29.09: «🔄 Сверить состав чата» (раздел «🔧 Управление»)
    "chclean:*": "settings",  # его галочки типов и задержка
    "state:ChatCleanupEdit:*": "settings",  # ввод задержки удаления
    "chpost:*": "settings",  # Квик 260927: пост рейтинга в чат — тумблер, день, проверка, публикация
    "state:ChatRatingPostEdit:*": "settings",  # ввод времени, числа мест, текстов поста
    "admin_sync_sheet": "settings",
    # Phase 33 (delegate-card admin actions): «🔍 Сверить с БД» — тот же класс экрана, что
    # «🔄 Синхронизация»/«♻️ Пересобрать таблицу» выше (handlers/sheets/admin_sheet_reconcile.py, всё
    # callback-пространство sheetrec_*: sheetrec_csv/sheetrec_append_confirm/sheetrec_append_go/
    # sheetrec_status_confirm/sheetrec_status_go).
    "admin_sheet_reconcile": "settings",
    "sheetrec_*": "settings",
    # Quick 260902-vth: «🕓 Журналы в таблицу» — та же капа, что «🔄 Синхронизация таблицы».
    "sheet_logs_open": "settings",
    "sheet_logs_autosync_toggle": "settings",
    "sheet_logs_sync_go": "settings",
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
    # Квик 260906-7zv (HELP-01/02/03): экран правки подсказки формата под вопросом
    # (reg_help_<step>, D-1 — ключ глобальный). "reg_help_rst:" заканчивается двоеточием и
    # потому не проглатывается префиксом "reg_help_rst_go:" — та же оговорка, что у соседней
    # пары reg_prompt_rst*.
    "reg_help_edit:*": "settings",
    "reg_help_rst:*": "settings",
    "reg_help_rst_go:*": "settings",
    "reg_prompt_edit:*": "settings",
    # Phase 25 (CITYQ-05): «↩️ Как везде» on the per-question text editor. Distinct literal
    # prefixes ("reg_prompt_rst:" ends in a colon before the go-variant's "_go") so, unlike
    # reg_q_reset_city/menu_reset_city, no exact-vs-prefix ordering hazard exists here — kept
    # anyway as two separate entries for the same reason those are.
    "reg_prompt_rst:*": "settings",
    "reg_prompt_rst_go:*": "settings",
    "reg_prompt_track:*": "settings",
    "reg_q_back": "settings",
    "reg_q_noop": "settings",
    "reg_q_ptoggle:*": "settings",
    # Phase 25 (CITYQ-04): «↩️ Как везде» on the header-scoped questions screen. Exact key
    # "reg_q_reset_city" MUST be registered ABOVE the prefixed "reg_q_reset_city_go:*" —
    # otherwise the exact key IS a string-prefix of the go-variant and, absent this ordering,
    # `required_capability`'s exact-match-first lookup would still resolve correctly (dict
    # membership checks the FULL string), but any future refactor toward pure prefix-scanning
    # must not swallow the exact entry — same reasoning already documented at
    # `menu_reset_city`/`menu_reset_city_go:*` above.
    "reg_q_reset_city": "settings",
    "reg_q_reset_city_go:*": "settings",
    "reg_q_stoggle:*": "settings",
    "reg_q_toggle:*": "settings",
    "reg_q_track:*": "settings",
    "reg_resume_mode_toggle": "settings",
    "roles_add": "settings",
    "roles_addrole:*": "settings",
    # Phase 33 (delegate-card admin actions, задача 2): прямой вход в мастер выдачи роли с
    # карточки /find (handlers/admin_roles.py::roles_add_for) — та же капа, что у остального
    # мастера выше.
    "roles_addfor:*": "settings",
    "roles_del:*": "settings",
    "roles_del_ok:*": "settings",
    # Phase 09.1 (C, ROLE-03): manager <-> city binding. "roles_city:*" does not swallow
    # "roles_city_pick:*" -- the prefixes diverge at the char right after "roles_city"
    # (":" vs "_"), same shape already documented for "roles_cap:*"/"roles_caps:*" above.
    "roles_city:*": "settings",
    "roles_city_pick:*": "settings",
    "roles_toggle:*": "settings",
    "settings_back": "settings",
    "settings_cancel": "settings",
    "settings_edit:*": "settings",
    "settings_edit_all:*": "settings",  # общая настройка при городе в шапке — ввод после кнопки
    "settings_enum_pick:*": "settings",
    # Phase 09.3 (06, CITY-09): header-scoped per-key editor — «✏️ Изменить для {город}»/
    # «↩️ Как везде» replace 09.2-05's four-entry per-city picker family (deleted from this
    # dict); same right as every other settings_* screen, never a separate capability.
    "settings_edit_city:*": "settings",
    "settings_reset_city:*": "settings",
    "settings_reset_city_go:*": "settings",
    "settings_file:*": "settings",
    # Quick 260822: списочные настройки по пунктам (handlers/admin_settings_lists.py) —
    # ➕ добавить / 🗑 выбрать пункт / 🗑 убрать выбранный / ✏️ заменить целиком. Тот же
    # «settings», что у settings_edit:*; prefix-ключи не пересекаются (del vs rm vs replace).
    "settings_list_add:*": "settings",
    "settings_list_del:*": "settings",
    "settings_list_rm:*": "settings",
    "settings_list_replace:*": "settings",
    "settings_list_attr:*": "settings",  # Phase 30 (30-07, задача 4): атрибуты списка-справочника
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
    # Подтверждение «пропала подстановка» при правке текста настройки.
    "phchk_*": "settings",
    # Quick 260919-mlu (Task 3): развилка «была своя вкладка с данными, имя меняется» —
    # переименовать / писать в существующую / завести новую пустую (отмена — sheets_tab_cancel
    # выше, тот же гейт).
    "sheet_tab_rename_go": "settings",
    "sheet_tab_reuse_go": "settings",
    "sheet_tab_newtab_go": "settings",
    # Quick 260919-mlu (Task 4): массовые кнопки «Добавить/Убрать префикс» на экране
    # «📄 Вкладки таблицы» — экран подтверждения + исполнение (отмена — sheets_tab_cancel).
    "sheet_tabs_prefix_add": "settings",
    "sheet_tabs_prefix_del": "settings",
    "sheet_tabs_prefix_add_go": "settings",
    "sheet_tabs_prefix_del_go": "settings",
    "settings_toggle_bonus": "settings",
    "settings_toggle_full_approval": "settings",
    "settings_toggle_notify": "settings",
    "settings_toggle_party_approval": "settings",
    "settings_toggle_reg": "settings",
    "settings_toggle_short_approval": "settings",
    "state:EditSetting:*": "settings",
    "state:SettingsSearch:*": "settings",  # «🔎 Найти настройку» — то же право, что у правки
    "settings_search": "settings",
    # «🚀 Первая настройка» в боте: настоящий гейт ADMIN_IDS — в handlers/admin_setup_wizard.py.
    "admin_setup_wizard": "settings", "setupw_*": "settings",
    "settings_search_cancel": "settings",
    "state:StaffAdd:*": "settings",
    "toggle_checkin_qr_enabled": "settings",  # Квик 260923 (форум-чекин)
    "toggle_consent_enabled": "settings",
    "toggle_consent_recollect": "settings",
    "toggle_delegate_lang_enabled": "settings",  # Phase 27
    "toggle_delegate_lang_ask_on_start": "settings",  # Phase 27
    "toggle_edu_conditional": "settings",
    "toggle_event_city_enabled": "settings",
    "toggle_nudge_enabled": "settings",
    "toggle_party_enabled": "settings",
    "toggle_party_fork_question": "settings",
    "toggle_payment_enabled": "settings",
    "toggle_preselect_enabled": "settings",
    "toggle_payment_reminders": "settings",
    "toggle_pending_reminder": "settings",
    "toggle_quiet_hours": "settings",
    "toggle_reg_edit_policy": "settings",  # Квик 260911-w2m
    "toggle_reg_resubmit_after_reject": "settings",  # Квик 260922-wrg
    "toggle_reg_edit_remoderation": "settings",
    "toggle_reg_skip_source_for_referred": "settings",  # Phase 28 (28-06)
    "toggle_reg_referrer_must_be_ambassador": "settings",  # Phase 28 (28-06)
    "toggle_reg_offer_ref_link": "settings",  # Phase 28 (28-06)
    "toggle_reg_scoring_enabled": "settings",  # Phase 28 (28-07)
    "toggle_resume_filename_short_mode": "settings",  # Phase 28 (28-09)
    "toggle_apps_queue_sort_by_score": "settings",  # Phase 28 (28-08)
    "toggle_reg_submit_notify": "settings",  # Квик 260916: дайджест заявок (раздел «📋 Заявки»)
    "toggle_daily_digest": "settings",  # Квик 260916: «📊 Итоги дня» (раздел «🔧 Управление»)
    "toggle_amb_team_selection": "settings",  # тумблер модуля «🤝 Отбор амбассадоров»
    "toggle_wave_rating_show_names": "settings",  # тумблер «имена в рейтинге волны»
    "toggle_show_progress": "settings",
    "toggle_uni_mode": "settings",
    # Phase 30 (30-01, A2-08): девять тумблеров «Анкета 2.0» — handlers/regform/admin_reg_form.py.
    "toggle_reg_form_v2": "settings",
    "toggle_reg_form_chips": "settings",
    "toggle_reg_form_lookup_search": "settings",
    "toggle_reg_form_edu_card": "settings",
    "toggle_reg_form_repeatable": "settings",
    "toggle_reg_form_limit_counter": "settings",
    "toggle_reg_form_status_screen": "settings",
    "toggle_reg_form_header_settings": "settings",
    "toggle_reg_form_haptics": "settings",
    # Phase 30 (30-07, A2-03): экран «📚 Справочники» — handlers/admin_lookup.py, весь
    # семейство callback'ов одним префиксом (`:*` покрывает kind/q/qm/qr/c/cu/cp/sel).
    "admin_lookup": "settings",
    "admin_lookup:*": "settings",
    "state:LookupAdmin:*": "settings",

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
    # Phase 32 (32-12, D-12/D-28): «Волна»/«Аудитория» button-choice steps -- new callbacks,
    # right after the city step in the wizard chain.
    "gtwave:*": "moderate_game",
    "gtaud:*": "moderate_game",
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
    # Phase 16 (16-04, GAME-UI-03): quick-pick сумм на шаге CoinsManual.amount.
    "coinsman_amount:*": "moderate_game",
    "state:CoinsManual:*": "moderate_game",

    # Phase 14 (14-05, GAME-09): «📜 Журнал монет» — paginated read screen + CSV export,
    # registered in the SAME commit as the handlers (09-01 convention).
    "admin_coins_journal": "moderate_game",
    "coinsjrn_page:*": "moderate_game",
    "coinsjrn_csv": "moderate_game",
    # Разовый перенос старых баллов из Google-таблицы (handlers/game/admin_coins_transfer.py).
    "admin_coins_transfer": "moderate_game",
    "cointr_*": "moderate_game",
    "state:CoinsTransfer:*": "moderate_game",

    # Phase 32 (32-10, D-06/D-09/D-10/D-11/D-13): экран «🌊 Волны» — список/создание/карточка/
    # правка/копия/активация/удаление (handlers/game/admin_game_waves.py). WARNING: та же ловушка
    # префиксов, что у gtdelete:*/gtdelete_go:* — "wavedel:*" НЕ покрывает "wavedel_go:*",
    # "waveactivate:*" НЕ покрывает "waveactivate_go:*", обе пары нужны отдельными строками.
    "admin_game_waves": "moderate_game",
    "wavenew": "moderate_game",
    "wavecopy": "moderate_game",
    "wavecopy:*": "moderate_game",
    "wavecopy_go:*": "moderate_game",
    "wave:*": "moderate_game",
    "waveedit:*": "moderate_game",
    "waveactivate:*": "moderate_game",
    "waveactivate_go:*": "moderate_game",
    "wavedel:*": "moderate_game",
    "wavedel_go:*": "moderate_game",
    # План 32-11 (D-16/D-17): экран итогов волны — кнопка приходит менеджеру ЛС (`services.
    # scheduler.send_wave_end_ping`), не с карточки волны, но обработчики живут в том же шве
    # (handlers/game/admin_game_waves.py). Та же ловушка префиксов: "wavefin:*" НЕ покрывает
    # "wavefin_go:*"/"wavefin_do:*" — три отдельные строки.
    "wavefin:*": "moderate_game",
    "wavefin_go:*": "moderate_game",
    "wavefin_do:*": "moderate_game",
    # Визардные callback'и (state-gated), их литералы ТОЖЕ резолвятся сторожем test_roles_
    # phase8.py отдельно от state-ключа — оба нужны, а не только "state:WaveCreate:*".
    "wcintro_skip": "moderate_game",
    "wcredates": "moderate_game",
    "wccreate_go": "moderate_game",
    "wccancel": "moderate_game",
    "state:WaveCreate:*": "moderate_game",
    "state:WaveEdit:*": "moderate_game",

    # Экран «🎓 Ступени амбассадоров» (handlers/amb/admin_amb_tiers.py). Та же ловушка префиксов:
    # "ambt_unexcl:*" НЕ покрывает "ambt_unexcl_go:*", "ambt_excl" не покрывает "ambt_excl_go".
    "admin_amb_tiers": "moderate_game",
    "ambt_toggle:*": "moderate_game",
    "ambt_csv": "moderate_game",
    "ambt_excl": "moderate_game",
    "ambt_excl_l": "moderate_game",
    "ambt_fill": "moderate_game",
    "ambt_fill_go:*": "moderate_game",
    "ambt_excl_go": "moderate_game",
    "ambt_excl_cancel": "moderate_game",
    "ambt_excl_list:*": "moderate_game",
    "ambt_unexcl:*": "moderate_game",
    "ambt_unexcl_go:*": "moderate_game",
    "state:AmbExclude:*": "moderate_game",
    # Раздел «🤝 Амбассадоры» (handlers/amb/admin_amb_section.py). Отдельного права не заводим: роли
    # «Менеджер регистраций» ставят галочку «🎮 Модерация геймификации» в «Ролях». Ловушка
    # префиксов: "ambs_mode" не покрывает "ambs_mode_go:*".
    "admin_amb_entry": "moderate_game",
    "ambs_mode": "moderate_game",
    "ambs_mode_go:*": "moderate_game",
    "ambs_limit": "moderate_game",
    "ambs_limit_cancel": "moderate_game",
    "ambs_texts": "moderate_game",
    "amb_sep": "moderate_game",  # подзаголовок внутри раздела, ничего не делает
    "state:AmbSlotsEdit:*": "moderate_game",
    # «🙋 Кандидаты и команда» (handlers/amb/admin_amb_candidates.py). Анкета человека — ПДн, как в
    # очереди заявок: moderate_reg. Префиксы: "ambc:*" не ловит "ambc_*", "ambc_rm:*" — "ambc_rm_go:*".
    "admin_amb_candidates": "moderate_game",
    "ambc:*": "moderate_game",
    "ambp:*": "moderate_game",
    "ambc_take:*": "moderate_game",
    "ambc_later:*": "moderate_game",
    "ambc_pack:*": "moderate_game",
    "ambc_slot:*": "moderate_game",
    "ambc_rm:*": "moderate_game",
    "ambc_rm_go:*": "moderate_game",
    "ambc_csv": "moderate_game",
    "ambc_card:*": "moderate_reg",
    # Массовые действия (handlers/amb/admin_amb_bulk.py): отказ всем, назначение, архив сезонов.
    # Префиксы: "ambc_decl" (точный) не ловит "ambc_decl_go:*"/"ambc_decl_no", "ambc_add" — "ambc_add_*".
    "ambc_decl": "moderate_game",
    "ambc_decl_go:*": "moderate_game",
    "ambc_decl_no": "moderate_game",
    "ambc_add": "moderate_game",
    "ambc_add_pick:*": "moderate_game",
    "ambc_add_go:*": "moderate_game",
    "ambc_add_cancel": "moderate_game",
    "ambc_arch_csv": "moderate_game",
    "ambrst": "moderate_game",
    "ambrst_p:*": "moderate_game",
    "ambrst_go:*": "moderate_game",
    "state:AmbAppoint:*": "moderate_game",
    # «Закрепить приглашённого» (handlers/amb/admin_amb_journal.py): «ambj_pick:*» не ловит
    # «ambj_go»/«ambj_cancel», поэтому каждый callback отдельной строкой.
    "admin_amb_attach": "moderate_game",
    "ambj_pick:*": "moderate_game",
    "ambj_go": "moderate_game",
    "ambj_cancel": "moderate_game",
    "state:AmbAttach:*": "moderate_game",
    # Лестница ступеней (handlers/amb/admin_amb_tier_ladder.py): «ambl_del» не ловит «ambl_del_go»,
    # «ambl_rev» — «ambl_rev_pick:*»/«ambl_rev_go:*», «ambl_prom:*» — «ambl_prom_go:*».
    "ambl:main": "moderate_game",
    "ambl_add": "moderate_game",
    "ambl_del": "moderate_game",
    "ambl_del_go": "moderate_game",
    "ambl_quota:*": "moderate_game",
    "ambl_req": "moderate_game",
    "ambl_rev": "moderate_game",
    "ambl_rev_cancel": "moderate_game",
    "ambl_rev_pick:*": "moderate_game",
    "ambl_rev_go:*": "moderate_game",
    "ambl_prom:*": "moderate_game",
    "ambl_prom_go:*": "moderate_game",
    "ambl_unrev": "moderate_game",
    "ambl_unrev_go:*": "moderate_game",
    "state:AmbTierRevoke:*": "moderate_game",
    # «💰 Баллы и приватность» (handlers/amb/admin_amb_points.py): «ambpt_coins» не ловит
    # «ambpt_coins_cancel», «ambpt_toggle:*» — префикс.
    "admin_amb_points": "moderate_game",
    "ambpt_coins": "moderate_game",
    "ambpt_coins_cancel": "moderate_game",
    "ambpt_toggle:*": "moderate_game",
    "ambpt_fill": "moderate_game",
    "ambpt_fill_go*": "moderate_game",  # и старая кнопка без чисел — ответ «Кнопка устарела»
    "state:AmbPointsEdit:*": "moderate_game",

    # Phase 12 (FORUM-CHECKIN.md): раздел «✅ Отметки на форуме» — счётчик + загрузка
    # выгрузки офлайн-сканера (handlers/admin_checkin.py). Первые реальные ключи капы
    # `checkin` — до этого она существовала в ALL_CAPABILITIES/ROLES без единой строки меню.
    "admin_checkin": "checkin",
    "checkin_upload_start": "checkin",
    "checkin_point:*": "checkin",
    # Форум-ночь п.5 (D-18): выбор города для точек-сессий загрузки CSV (менеджер без
    # закреплённого города/модуль включён) — та же капа «checkin», что у самого экрана.
    "checkin_point_city:*": "checkin",
    "state:CheckinImport:*": "checkin",

    # Форум-ночь B1 (идея №10): перевыпуск QR — кнопка на карточке «/find» (cmd_find_user, та
    # же капа «moderate_reg», что и у самой команды). Управление делегатским аккаунтом, не
    # рутинное сканирование на входе -- поэтому «moderate_reg», не «checkin».
    "checkin_reissue:*": "moderate_reg",
    "checkin_reissue_yes:*": "moderate_reg",
    "checkin_reissue_no": "moderate_reg",

    # Форум-ночь B4 (идея №8): «🧪 Проверить приложение-сканер» — та же капа «checkin», что у
    # раздела-владельца (ничего не отмечает, только парсит выгрузку и отвечает читаемостью).
    "checkin_test_start": "checkin",
    "checkin_test_qr": "checkin",
    "state:CheckinTestUpload:*": "checkin",
    # Бэклог чек-ина №7: «🧪 Учебные QR» — право «checkin» ИЛИ «moderate_reg». Карта знает одно
    # право на ключ, поэтому здесь «любое право панели», а пару проверяет сам хендлер
    # (handlers/admin_checkin_training.py). Лист ничего не пишет и прав не даёт.
    "checkin_training_sheet": ANY_CAPABILITY,

    # Форум-ночь п.3 (D-03, идея №2): рассылка QR перед форумом + её настройки — та же капа
    # «moderate_reg», что у перевыпуска QR выше (D-01, идея №10): массовая отправка сообщений
    # ВСЕМ делегатам города и правка расписания рассылки — не рутинное сканирование на входе,
    # которое разрешено волонтёру правом «checkin».
    "checkinqr_send:*": "moderate_reg",
    "checkinqr_send_go:*": "moderate_reg",
    "checkinqr_send_no": "moderate_reg",
    "checkinqr_cfg:*": "moderate_reg",
    "checkinqr_toggle:*": "moderate_reg",
    "checkinqr_time:*": "moderate_reg",
    "state:CheckinQrTimeEdit:*": "moderate_reg",

    # D-36 (24.09, аудит форумных тумблеров): «🎪 Форум: функции» — единый статус-экран (все
    # тумблеры в одном месте) + недостающий экран «Шпаргалка волонтёра накануне» (D-33).
    # Капа «moderate_reg» — тот же довод, что у checkinqr_cfg выше (массовая рассылка + правка
    # расписания, не рутинное сканирование волонтёра); отдельные строки хаба ссылаются на
    # экраны с ДРУГИМИ капами (admin_checkin/checkin, admin_menu_buttons/settings и т.д.) — та
    # же развилка «вход широкий, действие узкое», что у «⚙️ Настройки QR» на admin_checkin.
    "admin_forum_functions": "moderate_reg",
    "forumfn_city:*": "moderate_reg",
    # Приёмка 03.10: хаб «🎪 Форум: функции» — «🎟 Вход по QR» через экран подтверждения
    # (handlers/admin_forum_hub_nav.py), та же капа, что у самого тумблера; возврат в хаб — капа хаба.
    "forumfn_qr:*": "settings",
    "forumfn_qr_set:*": "settings",
    "forumfn_back:*": "moderate_reg",
    # Родные экраны, открытые из хаба (возврат — в хаб): капа та же, что у родного входа.
    "forumfn_open:*": "settings",  # запасной ключ; конкретные экраны ниже — по длинному префиксу
    "forumfn_open:chk:*": "checkin",
    "forumfn_open:app:*": "settings",
    "forumfn_open:menu:*": "settings",
    "forumfn_open:fb:*": "settings",
    "checkinvol_cfg:*": "moderate_reg",
    "checkinvol_toggle:*": "moderate_reg",
    "checkinvol_time:*": "moderate_reg",
    "state:CheckinVolGuideTimeEdit:*": "moderate_reg",

    # Идея №1 бэклога чек-ина (режим «день форума» главного меню делегата): та же капа
    # «moderate_reg», что и у остального хаба «🎪 Форум: функции» выше — своего родного экрана
    # раньше не было вовсе (ключи `forum_day_menu_enabled`/`forum_day_menu_start_time` жили
    # только в реестре), заведён этим же квиком.
    # Часовой пояс города (экран хаба «🎪 Форум: функции»): та же капа, что у соседних экранов.
    "forumtz_cfg:*": "moderate_reg",
    "forumtz_set:*": "moderate_reg",
    "forumdaymenu_cfg:*": "moderate_reg",
    "forumdaymenu_toggle:*": "moderate_reg",
    "forumdaymenu_time:*": "moderate_reg",
    "state:ForumDayMenuTimeEdit:*": "moderate_reg",

    # Идея №3 бэклога чек-ина (приветствие после первой отметки входа): та же капа
    # «moderate_reg», что и у остального хаба «🎪 Форум: функции» выше — тумблер-only экран
    # (сам текст `forum_welcome_text` правится generic-редактором «settings», см. докстринг
    # `handlers/admin_forum_functions.py` над `_welcome_cfg_text_kb`).
    "forumwelcome_cfg:*": "moderate_reg",
    "forumwelcome_toggle:*": "moderate_reg",

    # Идея №16 бэклога чек-ина (отчёт дня форума вечером): та же капа «moderate_reg», что и у
    # остального хаба «🎪 Форум: функции» выше — массовая отправка отчёта в чат оргов + личка
    # держателям moderate_reg, не рутинное сканирование волонтёра.
    "forumdayreport_cfg:*": "moderate_reg",
    "forumdayreport_toggle:*": "moderate_reg",
    "forumdayreport_time:*": "moderate_reg",
    "state:ForumDayReportTimeEdit:*": "moderate_reg",
    "forumdayreport_now:*": "moderate_reg",
    "forumdayreport_csv:*": "moderate_reg",

    # Идея №23 бэклога чек-ина (опрос неявившихся «почему не пришёл»): та же капа
    # «moderate_reg», что и у остального хаба выше — массовая рассылка опроса делегатам города.
    "forumnoshowpoll_cfg:*": "moderate_reg",
    "forumnoshowpoll_toggle:*": "moderate_reg",
    "forumnoshowpoll_time:*": "moderate_reg",
    "state:ForumNoshowPollTimeEdit:*": "moderate_reg",

    # Трек «региональные форумы → Москва»: та же капа «moderate_reg», что и у остального хаба
    # выше — массовая рассылка предложения делегатам региона + правка города/статуса переноса.
    "rgnm_cfg:*": "moderate_reg",
    "rgnm_toggle:*": "moderate_reg",
    "rgnm_time:*": "moderate_reg",
    "state:RegionalNoshowMoveTimeEdit:*": "moderate_reg",
    "rgnm_status_toggle:*": "moderate_reg",
    "rgnm_target_start:*": "moderate_reg",
    "rgnm_target_pick:*": "moderate_reg",

    # Форум-ночь п.6 (D-25, идея №14): шаблон «Не пришёл» — та же капа «moderate_reg», что у
    # рассылки QR выше (тот же довод: массовая отправка сообщений делегатам города, не
    # рутинное сканирование на входе, которое разрешено волонтёру правом «checkin»).
    "cna_send:*": "moderate_reg",
    "cna_send_go:*": "moderate_reg",
    "cna_send_no": "moderate_reg",
    # Идеи №31/№32: «📓 Журнал площадки» и снятие отметки менеджером (handlers/admin_venue.py) —
    # «moderate_reg», тот же довод, что у перевыпуска QR: правка чужих отметок, не сканирование.
    "admin_venue_log": "moderate_reg",
    "vlog:*": "moderate_reg",
    "vlogst": "moderate_reg",
    "vrv_find": "moderate_reg",
    "vrv_u:*": "moderate_reg",
    "vrv_p:*": "moderate_reg",
    "vrv_go:*": "moderate_reg",
    "state:VenueRevokeFind:*": "moderate_reg",

    # Бэклог чек-ина п.10: «📊 Статистика прихода» (handlers/admin_checkin_stats.py) — сводка по
    # всему городу для менеджера, не волонтёрская `checkin`; та же капа, что у соседних
    # менеджерских экранов чек-ина выше.
    "checkin_stats": "moderate_reg",
    "checkin_stats_refresh": "moderate_reg",
    "checkin_stats_csv": "moderate_reg",
    # Бэклог №12: «📍 Сейчас на площадке» (handlers/admin_checkin_floor.py) — та же капа.
    "checkin_floor": "moderate_reg",
    "checkin_floor_refresh": "moderate_reg",
    # Бэклог №25: «🚦 Готовность к форуму» (handlers/admin_forum_ready.py) — кнопка хаба «🎪 Форум:
    # функции», та же капа, что у хаба. Префиксы разные: "forum_ready:*" не покрывает "forum_ready_re:*".
    "forum_ready:*": "moderate_reg",
    "forum_ready_re:*": "moderate_reg",

    # Идея №6 бэклога чек-ина (права со сроком действия): экран «⏳ Срок действия роли»
    # (handlers/admin_roles.py) — та же капа, что весь остальной экран «👥 Роли и доступы»
    # (admin_roles выше).
    "rexp:*": "settings",
    "rexp_go:*": "settings",
    "rexp_custom:*": "settings",
    "state:RolesExpiryEdit:*": "settings",

    # Идея №5 бэклога чек-ина (приглашение волонтёров ссылкой): «🔗 Пригласить волонтёров»
    # (handlers/admin_volunteer_invite.py) — та же капа, что весь остальной хаб «🎪 Форум:
    # функции» (admin_forum_functions выше): массовая выдача доступа третьим лицам, не
    # рутинное сканирование, которого достаточно праву «checkin».
    "volinvite_entry": "moderate_reg",
    "volinvite_city_pick:*": "moderate_reg",
    "volinvite_cfg:*": "moderate_reg",
    "volinvite_toggle:*": "moderate_reg",
    "volinvite_new:*": "moderate_reg",
    "volinv_le:*": "moderate_reg",
    "volinv_re:*": "moderate_reg",
    "volinv_lim:*": "moderate_reg",
    "volinv_revoke:*": "moderate_reg",
    "volinv_revoke_go:*": "moderate_reg",
    "volinv_revoke_no:*": "moderate_reg",
    "volinv_users:*": "moderate_reg",
    "volinv_removeuser:*": "moderate_reg",
    "state:VolunteerInviteWizard:*": "moderate_reg",

    # Квик 260910-ro7 (DELU-01..08): скрытая команда «/delete_user» — то же положение, что у
    # «admin_season_reset»/«season_reset_go» выше: «settings» тут необходимо, но НЕ
    # достаточно — настоящий гейт `config.ADMIN_IDS`, повторно проверяется внутри КАЖДОГО из
    # трёх хендлеров handlers/admin_purge.py. Команда нигде не выведена в интерфейс — только
    # по точному имени.
    "cmd:delete_user": "settings",
    "delu_go:*": "settings",
    "delu_no": "settings",

    # Phase 33 (delegate-card admin actions): «🏙 Перевести в город» — кнопка на карточке
    # «/find» (handlers/cities/admin_city_move.py), та же капа «moderate_reg», что у самой команды
    # (`cmd:find` выше) и у соседнего «🔄 Перевыпустить QR» (checkin_reissue:*): управление
    # делегатским аккаунтом, не рутинное сканирование на входе.
    "citymv_start:*": "moderate_reg",
    "citymv_pick:*": "moderate_reg",
    "citymv_apply:*": "moderate_reg",
    "citymv_cancel:*": "moderate_reg",
    "citymv_notify:*": "moderate_reg",

    # Phase 33 (delegate-card admin actions): «↩️ Вернуть в ожидание» — та же капа «moderate_reg»,
    # что у соседнего «🏙 Перевести в город» выше (handlers/applications/admin_revert_pending.py).
    "revertp_start:*": "moderate_reg",
    "revertp_toggle:*": "moderate_reg",
    "revertp_apply:*": "moderate_reg",
    "revertp_cancel:*": "moderate_reg",
    # «📨 Отправить решение заново» — кнопка карточки /find (handlers/applications/admin_resend_decision.py).
    # Префикс decresend_ не пересекается ни с одним существующим ключом.
    "decresend_start:*": "moderate_reg",
    "decresend_go:*": "moderate_reg",
    "decresend_cancel:*": "moderate_reg",
    # Phase 33 (задача 2): «🔁 Разрешить повторную подачу» / отзыв — та же капа, что у соседних
    # карточных действий (handlers/applications/admin_resubmit_grant.py).
    "resubg_start:*": "moderate_reg",
    "resubg_toggle:*": "moderate_reg",
    "resubg_apply:*": "moderate_reg",
    "resubg_cancel:*": "moderate_reg",
    "resubg_revoke:*": "moderate_reg",
    # Phase 33 (задача 3): «✏️ Открыть правку после решения» / отзыв — та же капа, что у
    # соседних карточных действий (handlers/applications/admin_edit_grant.py).
    "editg_start:*": "moderate_reg",
    "editg_toggle:*": "moderate_reg",
    "editg_apply:*": "moderate_reg",
    "editg_cancel:*": "moderate_reg",
    "editg_revoke:*": "moderate_reg",
    # Phase 33 (delegate-card admin actions, задача 1): «🧹 Сбросить зависшую анкету» — та же
    # капа, что у соседних карточных действий (handlers/applications/admin_reg_reset.py).
    "regreset_start:*": "moderate_reg",
    "regreset_toggle:*": "moderate_reg",
    "regreset_apply:*": "moderate_reg",
    "regreset_cancel:*": "moderate_reg",
    # Phase 33 (delegate-card admin actions, задача 3): «📎 Заменить резюме» — та же капа, что
    # у соседних карточных действий (handlers/applications/admin_resume_replace.py); FSM-ожидание файла —
    # тот же приём group-wide wildcard, что у state:StaffAdd:* выше.
    "resumerep_start:*": "moderate_reg",
    "resumerep_cancel:*": "moderate_reg",
    "state:ResumeReplace:*": "moderate_reg",


    # Идея №20 бэклога чек-ина (бюро находок): мастер «🧳 Нашли вещь»
    # (handlers/admin_lost_found.py) — капа «checkin», тот же довод, что у admin_checkin выше
    # (рутинное действие волонтёра на площадке, не настройка и не массовая рассылка).
    "lost_found_new": "checkin",
    "cmd:found": "checkin",
    "state:LostFoundNew:*": "checkin",
    "lostfound_city_pick:*": "checkin",
    "lostfound_publish": "checkin",
    "lostfound_cancel": "checkin",
    # Экран тумблера «🧳 Бюро находок» — капа «moderate_reg», тот же довод, что у соседних
    # строк хаба «🎪 Форум: функции» (checkinvol_cfg:*/volinvite_cfg:* выше): включение
    # функции для города, не рутинное действие волонтёра.
    "lostfound_cfg:*": "moderate_reg",
    "lostfound_toggle:*": "moderate_reg",
    # Кнопка «✅ Нашёлся хозяин» живёт ПОД постом в группе делегатов, её видит и может
    # нажать любой участник чата (не только штат) — настоящий гейт («checkin» ИЛИ
    # «moderate_reg», обе аудитории по плану) не выражается ОДНИМ значением этой карты
    # (D-01/D-15: один ключ -> одна капа), поэтому запись ANY_CAPABILITY — тот же
    # навигационный приём, что «admin_city_pick:*»/«admin_city_switch*» выше: реальная
    # проверка (OR двух прав) — вручную внутри `handlers.admin_lost_found.lostfound_return`.
    "lostfound_return:*": ANY_CAPABILITY,
    # Идея №29 бэклога чек-ина («Твой Юлид в цифрах») — капа «moderate_reg», тот же довод, что
    # у соседних строк хаба «🎪 Форум: функции» (checkinvol_cfg:*/lostfound_cfg:* выше):
    # массовая рассылка + правка настроек экрана, не рутинное действие волонтёра.
    "forumstats_cfg:*": "moderate_reg",
    "forumstats_toggle:*": "moderate_reg",
    "forumstats_preview:*": "moderate_reg",
    "forumstats_pick:*": "moderate_reg",
    "forumstats_send_go:*": "moderate_reg",
    # D-41 (регистрация на месте): экран «📝 Регистрация на месте» (handlers/admin_onsite_reg.py)
    # — капа «moderate_reg», тот же довод, что у соседних тумблеров хаба «🎪 Форум: функции»
    # (lostfound_cfg:*/volinvite_cfg:*): включение функции для города и QR ссылки для стойки.
    "onsitereg_cfg:*": "moderate_reg",
    "onsitereg_toggle:*": "moderate_reg",
    "onsitereg_qr:*": "moderate_reg",
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
                         raw_state: str | None = None, special: str | None = None) -> str | tuple | None:
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


def _is_sos_reply_shape(message: Message) -> bool:
    """Реплай на карточку SOS (🆔+🆘) — без этой формы ответ на личную копию карточки падал в
    deny-by-default (`required=[None]`) даже у суперадмина."""
    replied = getattr(message, "reply_to_message", None)
    if not replied or not getattr(replied, "text", None):
        return False
    return "🆔" in replied.text and "🆘" in replied.text


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
    convention (docs/CONVENTIONS.md), which every existing admin handler already relies on via
    plain attribute access, never `isinstance`."""
    return hasattr(event, "data") and not hasattr(event, "text")


def _holds(user_caps: set, cap) -> bool:
    """ANY_CAPABILITY means 'any non-empty set'; a tuple — «любое из»; anything else is exact
    membership."""
    if cap == ANY_CAPABILITY:
        return bool(user_caps)
    if isinstance(cap, tuple):
        return any(c in user_caps for c in cap)
    return cap in user_caps


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
                shape = ("question_reply" if _is_question_reply_shape(event)
                         else "sos_reply" if _is_sos_reply_shape(event) else None)
                if shape is None:  # реплай в личке на копию дописки делегата SOS — без маркеров
                    from services.sos import is_relay_reply
                    shape = "sos_reply" if await is_relay_reply(event) else None
                required = [required_capability(special=shape) if shape else None]
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
