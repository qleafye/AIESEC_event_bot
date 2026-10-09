"""Phase 13 (13-04, REFAC-01): settings-guide + roles/staff seam.

The self-documenting `/settings_guide` screen (D-16, physically adjacent to roles and
preceding them in the original file, so grouping keeps the flattened handler order
contiguous per this plan's own guidance) plus «👥 Роли и доступы» (ROLE-02, D-18): role
on/off toggles, per-role capability checkboxes, and staff CRUD (add by id/username/forward,
per-city binding, assign/remove). Decorates the SAME shared `admin.router` (13-02/13-03
shared-router seam-import technique) — imported in the aggregator's bottom seam-import list
BEFORE admin_gamification (guide+roles precede gamification in the original file order).

`_resolve_staff_input`/`_STAFF_INPUT_ERROR` (this module's forward/@username person-lookup
parser) are also reused by `handlers/admin_gamification.py`'s `coinsman_person_step` — that
module imports them from HERE, not from the aggregator, since admin_roles is always imported
first (see handlers/admin.py's bottom seam-import order).
"""
import html as html_module
import logging

from aiogram import F, types, Bot
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove

from config import config
from settings_schema import get_setting_typed
from database.db import (
    add_staff,
    get_reg_started_by_id,  # Phase 33 (задача 2): экран назначения роли для «только /start»
    get_setting,
    get_staff_city,
    get_user,
    list_staff,
    remove_staff,
    set_staff_city,
    set_staff_expiry,
)
from services.person_label import msk_stamp_from_utc_iso, person_label
from services.person_search import search_people
from services.staff_reach import mark_text, superadmin_lines, unreachable_marks
from services.staff_expiry import (
    forum_end_date_iso,
    format_ddmmyyyy,
    is_expiry_active,
    parse_ddmmyyyy,
)
from settings_audit import set_setting_by_admin
from handlers.states import RolesExpiryEdit, StaffAdd
from handlers.admin_caps import (
    ALL_CAPABILITIES,
    CAP_LABELS,
    ROLES,
    capability_holders,
    has_capability,
    role_caps_key,
    role_enabled_key,
)
from services.checkin_volunteer_broadcast import greet_new_holder, greet_new_holders
from keyboards.builders import get_cancel_kb

logger = logging.getLogger(__name__)
from cities import (
    CITIES,
    cities_module_on,
    city_codes,
    city_label,
    default_city_code,
    is_city_enabled,
)
from handlers.admin import router

# ── Phase 2: self-documenting settings command (D-16) ────────────────────────
# Quick 260726-0bc: the guide used to dump raw bot_settings keys ("pending_notify_mode —
# instant/batched"). Managers do not read keys — they read screens. It now renders as
# grouped plain-Russian cards: what the setting does, what it is set to RIGHT NOW in words,
# and which admin button changes it. `entries` carry the raw key only for the DB read.
#
# Phase 20 (20-05, ADMIN-IA-04): поле `where` начинается с подписи РАЗДЕЛА из
# `handlers/admin_sections.py::SECTIONS` — экрана «⚙️ Настройки форума» со всеми тумблерами
# больше нет, и путь без раздела никуда не ведёт. Сторож
# `tests/test_admin_sections_ia20.py::test_settings_guide_where_starts_with_a_real_section`
# роняет набор, если раздел переименуют, а справку забудут.

_GUIDE_ONOFF = {"on": "✅ включено", "off": "❌ выключено"}


def _guide_text_value(raw):
    """Human 'current value' for free-text settings — managers care whether a custom text is
    set at all, not about its full body (which can be several screens long)."""
    if raw is None or not str(raw).strip():
        return None
    text = " ".join(str(raw).split())
    return f"свой текст («{text[:60]}…»)" if len(text) > 60 else f"свой текст («{text}»)"


# Each entry: key + label + what it does + how to read its value + where to change it.
# `values` maps a stored value to a human phrase; missing/unknown values fall through to
# `default` (shown with the «по умолчанию» marker) or to _guide_text_value for text settings.
SETTINGS_GUIDE_SECTIONS = [
    (
        "📝 Приём заявок",
        "Какая анкета показывается и нужно ли одобрение менеджера.",
        [
            {
                "key": "registration_mode",
                "label": "Форма регистрации",
                "what": "Краткая — отдельная настраиваемая анкета (акция) со своей вкладкой "
                        "Google-таблицы. Полная — обычная анкета, свой лист. Набор вопросов "
                        "краткой формы настраивается отдельно (📝 Анкета → «📋 Вопросы "
                        "регистрации» → «⚡ Краткая»), пресет «⚡ Акция: 6 вопросов» включает "
                        "ФИО, телефон, ВК, город, образование, курс.",
                "values": {"short": "⚡ краткая (акционная анкета)", "full": "📋 полная анкета"},
                "default": "short",
                "where": "📝 Анкета → тумблер «📝 Регистрация: 📋 Полная / ⚡ Краткая»",
            },
            {
                "key": "short_sheet_tab",
                "label": "Вкладка для краткой формы",
                "what": "Куда падают заявки краткой (акционной) формы — отдельная вкладка "
                        "Google-таблицы, основной лист не трогает.",
                "default": "Краткая",
                "where": "📊 Данные → «📄 Вкладки таблицы» → «⚡ Краткая форма (акция)»",
            },
            {
                "key": "short_approval",
                "label": "Одобрение для краткой формы",
                "what": "Авто — участник сразу попадает в меню. Вручную — ждёт менеджера.",
                "values": {"auto": "автоматически", "manual": "вручную (через «📋 Заявки»)"},
                "default": "auto",
                "where": "📋 Заявки → тумблер «✅ Краткая форма»",
            },
            {
                "key": "full_approval",
                "label": "Одобрение для полной формы",
                "what": "Авто — участник сразу попадает в меню. Вручную — ждёт менеджера.",
                "values": {"auto": "автоматически", "manual": "вручную (через «📋 Заявки»)"},
                "default": "manual",
                "where": "📋 Заявки → тумблер «✅ Полная форма»",
            },
            {
                "key": "reg_q_resume",
                "label": "Просить резюме",
                "what": "Участник прикладывает PDF/DOCX или пишет резюме текстом.",
                "values": _GUIDE_ONOFF,
                "default": "off",
                "where": "📝 Анкета → «📋 Вопросы регистрации» → «📄 Резюме»",
            },
            {
                "key": "reject_text",
                "label": "Текст при отклонении заявки",
                "what": "Что участник прочитает, если менеджер нажал «❌ Отклонить».",
                "default": "стандартный «К сожалению, твоя заявка отклонена»",
                "where": "📋 Заявки → «⚙️ Тексты и настройки» → «🚫 При отклонении»",
            },
        ],
    ),
    (
        "🔔 Уведомления менеджерам о заявках",
        "Как часто бот дёргает вас, когда приходят новые заявки.",
        [
            {
                "key": "pending_notify_mode",
                "label": "Когда сообщать о новой заявке",
                "what": "О каждой — сообщение на каждую заявку. Сводкой по таймеру — одна сводка «Заявок: N».",
                "values": {"instant": "о каждой заявке", "batched": "сводкой по таймеру"},
                "default": "batched",
                "where": "📋 Заявки → тумблер «🔔 О новых заявках»",
            },
            {
                "key": "pending_reminder_enabled",
                "label": "Напоминать о нерассмотренных заявках",
                "what": "Бот периодически пишет, сколько заявок ждёт решения.",
                "values": _GUIDE_ONOFF,
                "default": "on",
                "where": "📋 Заявки → тумблер «📋 Сводка о заявках»",
            },
            {
                "key": "pending_reminder_interval",
                "label": "Как часто напоминать",
                "what": "Интервал сводки. 900 = 15 мин, 1800 = 30 мин, 3600 = 1 час.",
                "default": "1800 (30 минут)",
                "unit": " сек.",
                "where": "📋 Заявки → «⚙️ Тексты и настройки» → «🕒 Тайминг батчей заявок»",
            },
        ],
    ),
    (
        "⏰ Напоминания тем, кто бросил анкету",
        "Человек начал регистрацию и не дошёл до конца — бот сам его вернёт.",
        [
            {
                "key": "nudge_enabled",
                "label": "Догонять брошенные анкеты",
                "what": "Одно напоминание на человека, повторно бот не пишет.",
                "values": _GUIDE_ONOFF,
                "default": "on",
                "where": "📋 Заявки → тумблер «⏰ Догонялка анкет»",
            },
            {
                "key": "nudge_after_minutes",
                "label": "Через сколько напомнить",
                "what": "Сколько минут молчания участника ждать до напоминания.",
                "default": "120 (2 часа)",
                "unit": " мин.",
                "where": "📋 Заявки → «⚙️ Тексты и настройки» → «⏰ Догонялка: через сколько минут»",
            },
            {
                "key": "nudge_text",
                "label": "Текст напоминания",
                "what": "Что получит человек, бросивший анкету.",
                "default": "стандартный текст",
                "where": "📋 Заявки → «⚙️ Тексты и настройки» → «⏰ Догонялка: текст напоминания»",
            },
        ],
    ),
    (
        "🎯 Предотбор по Google-таблице",
        "Пускать в бота только тех, кто уже отобран вручную (список @username в таблице). "
        "Если таблица недоступна — бот пускает всех и пишет админу, регистрация не встаёт.",
        [
            {
                "key": "preselect_enabled",
                "label": "Предотбор",
                "what": "Проверять @username по вкладке с отобранными на входе.",
                "values": _GUIDE_ONOFF,
                "default": "off",
                "where": "📋 Заявки → тумблер «🎯 Предотбор по таблице»",
            },
            {
                "key": "preselect_tab",
                "label": "Вкладка со списком отобранных",
                "what": "Имя вкладки в вашей Google-таблице, где лежат @username.",
                "default": "Отобранные",
                "where": "📊 Данные → «📄 Вкладки таблицы» → «🎯 Отобранные (предотбор)»",
            },
            {
                "key": "preselect_fail_text",
                "label": "Текст не прошедшим отбор",
                "what": "Что увидит человек, которого нет в списке.",
                "default": "«Отбор не пройден.»",
                "where": "📋 Заявки → «⚙️ Тексты и настройки» → «🎯 Предотбор: не прошёл»",
            },
            {
                "key": "preselect_link",
                "label": "Ссылка не прошедшим отбор",
                "what": "Куда отправить человека, если он не в списке (канал, сайт).",
                "default": "нет",
                "where": "📋 Заявки → «⚙️ Тексты и настройки» → «🎯 Предотбор: ссылка»",
            },
        ],
    ),
    (
        "👥 Роли и доступы",
        "Кто, кроме суперадминов из .env, имеет доступ к админке, и что именно каждой роли "
        "можно (подробности — docs/ADMIN_GUIDE.md, §22).",
        [
            {
                "key": "role_reg_manager_enabled",
                "label": "Роль «Менеджер регистраций»",
                "what": "Выключенная роль не даёт прав никому из её носителей, но сами люди "
                        "остаются в списке.",
                "values": _GUIDE_ONOFF,
                "default": "on",
                "where": "🔧 Управление → «👥 Роли и доступы»",
            },
            {
                "key": "role_caps_reg_manager",
                "label": "Права роли «Менеджер регистраций»",
                "what": "Список прав, по одному на строке (или через «;»): moderate_reg, "
                        "moderate_receipts, moderate_game, broadcast, settings, stats, checkin.",
                "default": "moderate_reg, moderate_receipts",
                "where": "🔧 Управление → «👥 Роли и доступы» → «✏️ Права роли: 🛂 Менеджер регистраций»",
            },
            {
                "key": "role_game_manager_enabled",
                "label": "Роль «Менеджер геймификации»",
                "what": "Выключенная роль не даёт прав никому из её носителей, но сами люди "
                        "остаются в списке.",
                "values": _GUIDE_ONOFF,
                "default": "on",
                "where": "🔧 Управление → «👥 Роли и доступы»",
            },
            {
                "key": "role_caps_game_manager",
                "label": "Права роли «Менеджер геймификации»",
                "what": "Список прав, по одному на строке (или через «;»): moderate_reg, "
                        "moderate_receipts, moderate_game, broadcast, settings, stats, checkin.",
                "default": "moderate_game",
                "where": "🔧 Управление → «👥 Роли и доступы» → «✏️ Права роли: 🎮 Менеджер геймификации»",
            },
            {
                "key": "role_stats_manager_enabled",
                "label": "Роль «Менеджер статистики»",
                "what": "Выключенная роль не даёт прав никому из её носителей, но сами люди "
                        "остаются в списке.",
                "values": _GUIDE_ONOFF,
                "default": "on",
                "where": "🔧 Управление → «👥 Роли и доступы»",
            },
            {
                "key": "role_caps_stats_manager",
                "label": "Права роли «Менеджер статистики»",
                "what": "Список прав, по одному на строке (или через «;»): moderate_reg, "
                        "moderate_receipts, moderate_game, broadcast, settings, stats, checkin.",
                "default": "stats",
                "where": "🔧 Управление → «👥 Роли и доступы» → «✏️ Права роли: 📊 Менеджер статистики»",
            },
            {
                "key": "role_volunteer_enabled",
                "label": "Роль «Волонтёр форума»",
                "what": "Выключенная роль не даёт прав никому из её носителей, но сами люди "
                        "остаются в списке.",
                "values": _GUIDE_ONOFF,
                "default": "on",
                "where": "🔧 Управление → «👥 Роли и доступы»",
            },
            {
                "key": "role_caps_volunteer",
                "label": "Права роли «Волонтёр форума»",
                "what": "Список прав, по одному на строке (или через «;»): moderate_reg, "
                        "moderate_receipts, moderate_game, broadcast, settings, stats, checkin.",
                "default": "checkin",
                "where": "🔧 Управление → «👥 Роли и доступы» → «✏️ Права роли: 🎗 Волонтёр форума»",
            },
            {"key": "role_reg_volunteer_enabled", "label": "Роль «Волонтёр регистрации»", "what": "Выключенная роль не даёт прав никому из её носителей, но сами люди остаются в списке.", "values": _GUIDE_ONOFF, "default": "on", "where": "🔧 Управление → «👥 Роли и доступы»"},
            {"key": "role_caps_reg_volunteer", "label": "Права роли «Волонтёр регистрации»", "what": "Отметка входа и одобрение человека у стойки (checkin, checkin_approve).", "default": "checkin; checkin_approve", "where": "🔧 Управление → «👥 Роли и доступы» → «✏️ Права роли: 🎗 Волонтёр регистрации»"},
            {"key": "role_marketing_manager_enabled", "label": "Роль «Маркетинг (метки)»", "what": "Выключенная роль не даёт прав никому из её носителей, но сами люди остаются в списке.", "values": _GUIDE_ONOFF, "default": "on", "where": "🔧 Управление → «👥 Роли и доступы»"},
            {"key": "role_caps_marketing_manager", "label": "Права роли «Маркетинг (метки)»", "what": "Только ссылки с метками и число анкет по каждой метке: в /admin — «📊 Данные» с «🔗 Ссылки с метками» и «📈 Источники»; ни заявок, ни контактов, ни настроек.", "default": "source_links", "where": "🔧 Управление → «👥 Роли и доступы» → «✏️ Права роли: 📣 Маркетинг (метки)»"},
        ],
    ),
    # Phase 32 (32-13, D-04): амбассадорские волны — компактно, чтобы не поднимать потолок файла.
    (
        "🤝 Амбассадоры: настройки",
        "Способ входа, места, ступени, баллы за приглашённых, призовые места, тексты волн.",
        [
            {"key": "ambassador_referral_coins", "label": "Баллы за приглашённого", "what": "За КАЖДУЮ одобренную заявку приглашённого амбассадору; 0 — выключено, назад не снимается.", "default": "0", "where": "🤝 Амбассадоры → «💰 Баллы и приватность» → «💰 Баллов за приглашённого»"},
            {"key": "game_late_penalty_percent", "label": "Штраф за просрочку задания", "what": "Процент от суммы задания при сдаче после дедлайна, виден делегату заранее на карточке.", "default": "0", "where": "🎮 Геймификация → «⚙️ Тексты и настройки» → «⏰ Штраф за сдачу после дедлайна, %»"},
            {"key": "wave_prize_places", "label": "Призовых мест в волне", "what": "Сколько верхних строк рейтинга волны станут призовыми при объявлении итогов.", "default": "3", "where": "🤝 Амбассадоры → «⚙️ Тексты и настройки» → «🏅 Призовых мест в волне»"},
            {"key": "wave_rating_show_names", "label": "Имена в рейтинге волны", "what": "Выключено — амбассадор видит только своё место и отрыв от призового, без чужих имён.", "values": _GUIDE_ONOFF, "default": "on", "where": "🤝 Амбассадоры → «💰 Баллы и приватность» → «🏅 Имена в рейтинге волны»"},
            {"key": "wave_start_message_text", "label": "Текст старта волны", "what": "ЛС каждому действующему амбассадору при старте волны: вводный текст + список заданий.", "default": "стандартный текст", "where": "🤝 Амбассадоры → «⚙️ Тексты и настройки» → «🌊 Старт волны: текст»"},
            {"key": "amb_qualified_program", "label": "Ступени амбассадоров СкиллАп", "what": "Засчитываем только прошедших отбор; за 1/3/7 таких — ступени с сообщением амбассадору.", "values": _GUIDE_ONOFF, "default": "off", "where": "🤝 Амбассадоры → «🎓 Ступени амбассадоров» → кнопка «🎓 Программа»"},
            {"key": "amb_hide_invitee_names", "label": "Скрывать имена приглашённых", "what": "Амбассадор видит только цифры: всего, на рассмотрении, прошли отбор; в истории баллов — «Приглашённый №N».", "values": _GUIDE_ONOFF, "default": "off", "where": "🤝 Амбассадоры → «💰 Баллы и приватность» → «🙈 Скрывать имена приглашённых»"},
            {"key": "amb_tier1_threshold", "label": "Ступень 1: порог", "what": "Сколько прошедших отбор нужно для подтверждённого статуса и сертификата.", "default": "1", "where": "🤝 Амбассадоры → «⚙️ Тексты и настройки» → «🎓 Ступень 1: сколько прошедших отбор»"},
            {"key": "amb_tier2_threshold", "label": "Ступень 2: порог", "what": "Сколько прошедших отбор нужно для разбора резюме.", "default": "3", "where": "🤝 Амбассадоры → «⚙️ Тексты и настройки» → «🎓 Ступень 2 (разбор резюме): сколько прошедших»"},
            {"key": "amb_tier3_threshold", "label": "Ступень 3: порог", "what": "Сколько прошедших отбор нужно для закрытого нетворкинга.", "default": "7", "where": "🤝 Амбассадоры → «⚙️ Тексты и настройки» → «🎓 Ступень 3 (нетворкинг): сколько прошедших»"},
            {"key": "amb_o2o_quota", "label": "Ступень 2: сколько наград", "what": "Раздаются по времени достижения ступени 2, сверх квоты — лист ожидания.", "default": "15", "where": "🤝 Амбассадоры → «⚙️ Тексты и настройки» → «🎓 Слотов на разбор резюме»"},
            {"key": "amb_count_deadline", "label": "Дедлайн ступеней", "what": "После этого момента (МСК) новые ступени не выдаются, прогресс считается дальше. «нет» — без дедлайна.", "default": "2026-11-14 23:59", "where": "🤝 Амбассадоры → «⚙️ Тексты и настройки» → «🎓 До какого момента выдаём ступени»"},
            {"key": "amb_join_mode", "label": "Как становятся амбассадорами", "what": "Сразу по кнопке — делегат попадает в команду сам; отбор менеджером — становится кандидатом, а в команду его берёте вы.", "values": {"instant": "⚡ Сразу по кнопке", "selection": "🗳 Отбор менеджером"}, "default": "instant", "where": "🤝 Амбассадоры → «🚪 Вход и лимит» → «Способ входа»"},
            {"key": "amb_slots_limit", "label": "Мест в команде (пакетов)", "what": "Сколько амбассадоров можно взять; когда места кончились, новые ждут в списке кандидатов. 0 — без лимита.", "default": "0", "where": "🤝 Амбассадоры → «🚪 Вход и лимит» → «🎁 Мест в команде»"},
            {"key": "amb_tiers_count", "label": "Сколько ступеней", "what": "Лестница наград амбассадора, от 1 до 5 ступеней; у каждой свой порог приглашённых.", "default": "3", "where": "🤝 Амбассадоры → «🎓 Ступени амбассадоров» → «🪜 Лестница ступеней» → «➕ Добавить ступень» / «➖ Убрать последнюю»"},
            {"key": "amb_tiers_require_approved", "label": "Ступени только с одобренной заявкой", "what": "Да — в зачёт ступеней идут только приглашённые с одобренной заявкой; нет — все поданные.", "values": _GUIDE_ONOFF, "default": "on", "where": "🤝 Амбассадоры → «🎓 Ступени амбассадоров» → «🪜 Лестница ступеней» → «Только с одобренной заявкой»"},
        ],
    ),
]

# Every key the guide needs to read from bot_settings (single source for the DB fetch).
SETTINGS_GUIDE_KEYS = [
    entry["key"] for _, __, entries in SETTINGS_GUIDE_SECTIONS for entry in entries
]

_GUIDE_CHUNK_LIMIT = 3500  # Telegram hard limit is 4096; leave room for HTML tags


def _render_guide_entry(entry: dict, raw) -> str:
    """One settings card: label, what it does, what it is set to now, where to change it."""
    values = entry.get("values") or {}
    if raw is not None and str(raw).strip():
        stored = str(raw).strip()
        if values:
            shown = values.get(stored, stored)
        else:
            unit = entry.get("unit")
            shown = f"{stored}{unit}" if unit and stored.isdigit() else _guide_text_value(raw)
    else:
        # Unset key → show what the bot actually does today, in the same human wording.
        default = entry["default"]
        shown = f"{values.get(default, default)} — по умолчанию"

    return (
        f"<b>{html_module.escape(entry['label'])}</b>\n"
        f"{html_module.escape(entry['what'])}\n"
        f"Сейчас: <b>{html_module.escape(str(shown))}</b>\n"
        f"Где менять: {html_module.escape(entry['where'])}"
    )


def _render_settings_guide(sections: list, current: dict) -> list[str]:
    """Render the guide as a list of Telegram-sized messages (one or more sections each)."""
    blocks = ["📖 <b>Справка: что где настраивается</b>"]
    for title, subtitle, entries in sections:
        block = [f"<b>{html_module.escape(title)}</b>\n{html_module.escape(subtitle)}"]
        block += [_render_guide_entry(e, current.get(e["key"])) for e in entries]
        blocks.append("\n\n".join(block).rstrip())
    blocks.append(
        "Остальное — внутри разделов /admin: 🎪 Событие (даты, место, контакты, приветствия, "
        "фото), 📝 Анкета (вопросы, тексты вопросов, согласия, Party), 📋 Заявки (тексты после "
        "подачи), 💳 Оплата, 📢 Общение, 🎮 Геймификация, 📊 Данные (таблица и выгрузки), "
        "🔧 Управление (города, роли, оформление).\nПодробный гайд — файл docs/ADMIN_GUIDE.md, "
        "короткая версия — docs/ADMIN_CHEATSHEET.md."
    )

    messages, buf = [], ""
    for block in blocks:
        candidate = f"{buf}\n\n{block}" if buf else block
        if len(candidate) > _GUIDE_CHUNK_LIMIT and buf:
            messages.append(buf)
            buf = block
        else:
            buf = candidate
    if buf:
        messages.append(buf)
    return messages


async def _send_settings_guide(target: types.Message):
    current = {key: await get_setting(key) for key in SETTINGS_GUIDE_KEYS}
    for chunk in _render_settings_guide(SETTINGS_GUIDE_SECTIONS, current):
        await target.answer(chunk, parse_mode="HTML")


@router.message(Command("settings_guide"))
async def cmd_settings_guide(message: types.Message):
    await _send_settings_guide(message)


@router.callback_query(F.data == "admin_settings_guide")
async def show_admin_settings_guide(callback: types.CallbackQuery):
    await _send_settings_guide(callback.message)
    await callback.answer()


# ── Phase 8 (ROLE-02, D-18): роли и доступы ─────────────────────────────────────────────
# Bespoke settings sub-screen (analog: show_reg_questions/render_questions_text/
# build_questions_keyboard) — reached from a hardcoded row in build_settings_keyboard, exactly
# like «📋 Вопросы регистрации». `staff` isn't a SETTINGS_SCHEMA row (D-11), so its CRUD needs
# real handlers; `role_caps_<role>` list editing rides the existing generic settings_edit flow
# for free (see settings_edit_start's registry-prompt fallback above).


def _expiry_line_text(expires_at: str | None) -> str:
    """Идея №6 бэклога чек-ина: строка в `render_roles_text` — «бессрочно» / «до 04.10» /
    «⌛ истекла 04.10» (D-6: ничего не удаляем, просто помечаем)."""
    if not expires_at:
        return "⏳ бессрочно"
    human = format_ddmmyyyy(expires_at) or expires_at
    return f"⏳ до {human}" if is_expiry_active(expires_at) else f"⌛ истекла {human}"


def _expiry_button_text(expires_at: str | None) -> str:
    """Короткая подпись кнопки в `build_roles_keyboard` — тот же формат, что строка списка,
    без ведущего «⏳»/«⌛» (кнопка уже несёт его сама, см. вызов)."""
    if not expires_at:
        return "Бессрочно"
    human = format_ddmmyyyy(expires_at) or expires_at
    return f"истекла {human}" if not is_expiry_active(expires_at) else f"до {human}"

async def render_roles_text() -> str:
    lines = ["👥 <b>Роли и доступы</b>", ""]
    for role, meta in ROLES.items():
        enabled = await get_setting_typed(role_enabled_key(role))
        state_label = "✅ Вкл" if enabled == "on" else "❌ Выкл"
        lines.append(f"{meta['label']}: <b>{state_label}</b>")
        # _known_caps отбрасывает сентинел пустого набора и мусор от старого текстового ввода —
        # показывать «—» надо ровно тогда, когда реальных прав нет.
        caps = _known_caps(await get_setting_typed(role_caps_key(role)))
        cap_text = ", ".join(CAP_LABELS.get(c, c) for c in caps) or "—"
        lines.append(f"　Права: {cap_text}")

    lines.append("")
    lines.append("<b>Люди</b>")
    staff = await list_staff()
    if not staff:
        lines.append("<i>Пока никто не назначен.</i>")
    show_city = await cities_module_on()  # Phase 09.1 (C, ROLE-03)
    marks = await unreachable_marks()  # 29.09: кому уведомления не доходят (fail-soft -> {})
    for row in staff:
        tid = row["telegram_id"]
        role_label = ROLES.get(row["role"], {}).get("label", row["role"])
        # Имя и @username вместо голого id; кто и когда выдал — тоже словами, время по Москве.
        name = html_module.escape(await person_label(tid))
        by = html_module.escape(await person_label(row["added_by"])) if row.get("added_by") else "—"
        line = f"• {name} — {role_label} (выдал(а) {by}, {msk_stamp_from_utc_iso(row.get('added_at'))})"
        if show_city:
            city = row.get("city")
            city_text = await city_label(city) if city else "🌍 Все города"
            line += f" · 🏙 {city_text}"
        line += f" · {_expiry_line_text(row.get('expires_at'))}"
        if tid in marks:
            line += f" · {mark_text(marks[tid].get('since'))}"
        lines.append(line)
    lines.extend(superadmin_lines(marks))

    lines.append("")
    admins_text = html_module.escape(", ".join([await person_label(a) for a in config.ADMIN_IDS]))
    lines.append(f"<i>Суперадмины ({admins_text}) имеют все права всегда; их список задаёт разработчик, из бота не снять.</i>")
    lines.append("⚠️ Право «⚙️ Настройки» включает управление ролями — выдавайте его как равнозначное админскому.")
    return "\n".join(lines)


async def build_roles_keyboard(viewer_id: int | None = None) -> InlineKeyboardMarkup:
    """`viewer_id=None` (default, back-compat for existing call sites/tests) = always show the
    🏙 city-edit button. WR-02 (09.1-REVIEW.md): the real enforcement lives in the
    `roles_city_start`/`roles_city_pick` handler gates below — this is only UX (CLAUDE.md: не
    показывать кнопку, которая заведомо откажет)."""
    buttons = []
    for role, meta in ROLES.items():
        enabled = await get_setting_typed(role_enabled_key(role))
        toggle_text = (
            f"{meta['label']}: ✅ Вкл → ❌ Выкл" if enabled == "on"
            else f"{meta['label']}: ❌ Выкл → ✅ Вкл"
        )
        buttons.append([InlineKeyboardButton(text=toggle_text, callback_data=f"roles_toggle:{role}")])
        buttons.append([InlineKeyboardButton(
            text=f"✏️ Права роли: {meta['label']}",
            callback_data=f"roles_caps:{role}",
        )])

    show_city = await cities_module_on()  # Phase 09.1 (C, ROLE-03)
    for row in await list_staff():
        tid = row["telegram_id"]
        role = row["role"]
        role_label = ROLES.get(role, {}).get("label", role)
        name = await person_label(tid)
        row_buttons = [InlineKeyboardButton(
            text=f"➖ {name} — {role_label}", callback_data=f"roles_del:{tid}:{role}",
        )]
        if show_city and (viewer_id is None or viewer_id in config.ADMIN_IDS):
            city = row.get("city")
            city_text = await city_label(city) if city else "🌍 Все города"
            row_buttons.append(InlineKeyboardButton(
                text=f"🏙 {city_text}", callback_data=f"roles_city:{tid}",
            ))
        # Идея №6 бэклога чек-ина: срок действия — поменять/снять у уже выданной роли, тот же
        # экран, что открывается сразу после выдачи (см. roles_assign ниже).
        row_buttons.append(InlineKeyboardButton(
            text=f"⏳ {_expiry_button_text(row.get('expires_at'))}",
            callback_data=f"rexp:{tid}:{role}",
        ))
        buttons.append(row_buttons)

    buttons.append([InlineKeyboardButton(text="➕ Добавить менеджера", callback_data="roles_add")])
    # Идея №5 бэклога чек-ина: вход в приглашение волонтёров ссылкой — тот же экран, что и
    # строка хаба «🎪 Форум: функции» (handlers/admin_volunteer_invite.py), просто другая
    # точка входа. Строковый callback_data, не импорт модуля — тот же приём, что ADMIN_CAPS
    # ссылается на чужие callback'и без импорта их модулей. Капа у самого экрана —
    # `volinvite_entry: "moderate_reg"` — ШИРЕ, чем «settings» этого экрана целиком, поэтому
    # кнопка видна только тому, кто её реально пройдёт (CLAUDE.md: не показывать кнопку,
    # которая заведомо откажет) — тот же приём, что show_city ниже смотрит на viewer_id.
    if viewer_id is not None and await has_capability(viewer_id, "moderate_reg"):
        buttons.append([InlineKeyboardButton(text="🔗 Пригласить волонтёров", callback_data="volinvite_entry")])
    # Phase 20 (20-03): «Назад» ведёт в раздел-владелец экрана («🔧 Управление»).
    from handlers.admin_sections import back_button  # ленивый шов: модульный импорт даст цикл
    buttons.append([back_button("admin_roles")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data == "admin_roles")
async def show_roles(callback: types.CallbackQuery):
    text = await render_roles_text()
    kb = await build_roles_keyboard(callback.from_user.id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("roles_toggle:"))
async def toggle_role_enabled(callback: types.CallbackQuery, bot: Bot | None = None):
    role = callback.data.split(":", 1)[1]
    if role not in ROLES:
        await callback.answer("Неизвестная роль", show_alert=True)
        return

    # Same body as _toggle_module_setting, but redraws the ROLES screen, not admin_settings.
    key = role_enabled_key(role)
    current = await get_setting_typed(key)
    new_val = "off" if current == "on" else "on"
    before = set(await capability_holders("checkin"))
    await set_setting_by_admin(callback.from_user.id, key, new_val)
    await greet_new_holders(bot, before)  # включённая роль могла нести checkin
    label = "✅ Вкл" if new_val == "on" else "❌ Выкл"
    await callback.answer(f"{ROLES[role]['label']}: {label}", show_alert=True)

    text = await render_roles_text()
    kb = await build_roles_keyboard(callback.from_user.id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


# ── Права роли: экран с чекбоксами вместо ввода кодов текстом (quick 260813) ───────────────
#
# Раньше «✏️ Права роли» вела в generic settings_edit: менеджеру показывали подсказку
# «moderate_reg, moderate_receipts, moderate_game, broadcast, settings, stats, checkin» и
# просили НАБРАТЬ нужные коды через «;». Это ровно тот случай, который запрещает главное
# правило проекта (CLAUDE.md): настройка из фиксированного набора обязана быть кнопками, а
# кодовые значения человеку показывать нельзя.
#
# Хранение не меняется: тот же ключ role_caps_<role>, тот же type:"list" в SETTINGS_SCHEMA —
# запись идёт «по одному коду на строке», что _parse_setting уже понимает. Значит откат к
# старому экрану не требует миграции данных.
#
# Пустой набор пишется СЕНТИНЕЛОМ, а не пустой строкой: _parse_setting для type:"list"
# возвращает `default` на falsy raw (settings_schema.py), то есть пустая строка молча вернула
# бы роли права по умолчанию — противоположность тому, что нажал менеджер. Сентинел не входит
# в ALL_CAPABILITIES, а resolve_capabilities отбрасывает всё, чего там нет
# (handlers/admin_caps.py) — на выходе честный нулевой набор прав.
_CAPS_EMPTY_SENTINEL = "—"


def _known_caps(raw_caps) -> list[str]:
    """Отфильтровать сентинел и любой мусор, оставшийся от прежнего текстового ввода."""
    return [c for c in (raw_caps or []) if c in ALL_CAPABILITIES]


async def render_role_caps_text(role: str) -> str:
    meta = ROLES[role]
    caps = _known_caps(await get_setting_typed(role_caps_key(role)))
    lines = [
        f"🛂 <b>Права роли: {meta['label']}</b>",
        "",
        "Отметьте, что этой роли можно делать. Нажатие сразу сохраняется.",
        "",
    ]
    if caps:
        lines.append("Сейчас разрешено: " + ", ".join(CAP_LABELS.get(c, c) for c in caps))
    else:
        lines.append("Сейчас роль <b>не может ничего</b> — ни одно право не отмечено.")
    lines.append("")
    lines.append(
        "⚠️ «⚙️ Настройки» — это и управление ролями тоже: у кого есть это право, тот может "
        "выдать права кому угодно, включая себя."
    )
    return "\n".join(lines)


def build_role_caps_keyboard(role: str, caps: list[str]) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(
            text=("✅ " if cap in caps else "☐ ") + CAP_LABELS.get(cap, cap),
            callback_data=f"roles_cap:{role}:{cap}",
        )]
        for cap in ALL_CAPABILITIES
    ]
    buttons.append([InlineKeyboardButton(text="← К ролям", callback_data="admin_roles")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def _show_role_caps(callback: types.CallbackQuery, role: str):
    caps = _known_caps(await get_setting_typed(role_caps_key(role)))
    await callback.message.edit_text(
        await render_role_caps_text(role),
        parse_mode="HTML",
        reply_markup=build_role_caps_keyboard(role, caps),
    )


@router.callback_query(F.data.startswith("roles_caps:"))
async def show_role_caps(callback: types.CallbackQuery):
    role = callback.data.split(":", 1)[1]
    if role not in ROLES:
        await callback.answer("Неизвестная роль", show_alert=True)
        return
    await _show_role_caps(callback, role)
    await callback.answer()


@router.callback_query(F.data.startswith("roles_cap:"))
async def toggle_role_cap(callback: types.CallbackQuery, bot: Bot | None = None):
    parts = callback.data.split(":")
    if len(parts) != 3:
        await callback.answer("Неизвестная кнопка", show_alert=True)
        return
    _, role, cap = parts
    if role not in ROLES or cap not in ALL_CAPABILITIES:
        await callback.answer("Неизвестное право", show_alert=True)
        return

    caps = _known_caps(await get_setting_typed(role_caps_key(role)))
    if cap in caps:
        caps.remove(cap)
        toast = f"{CAP_LABELS.get(cap, cap)}: снято"
    else:
        # Порядок как в ALL_CAPABILITIES, а не «в порядке нажатий» — чтобы строка прав в
        # render_roles_text не прыгала между перерисовками.
        caps = [c for c in ALL_CAPABILITIES if c == cap or c in caps]
        toast = f"{CAP_LABELS.get(cap, cap)}: разрешено"

    before = set(await capability_holders("checkin"))
    await set_setting_by_admin(
        callback.from_user.id, role_caps_key(role), "\n".join(caps) if caps else _CAPS_EMPTY_SENTINEL,
    )
    await greet_new_holders(bot, before)  # право checkin выдали существующей роли
    await callback.answer(toast)
    await _show_role_caps(callback, role)


_STAFF_INPUT_ERROR = (
    "Не понял, кого добавить. Пришлите пересланное сообщение от человека, "
    "@username или числовой id."
)


def _resolve_staff_input(message) -> tuple[int | None, str | None]:
    """Synchronous, DB-free parse of the admin's "who to add" input (docs/CONVENTIONS.md
    `_private`-helper unit-testability idiom — mirrors `_parse_coins_amount`).

    Returns (telegram_id, marker_or_error):
    - (id, None) — resolved directly (forward or numeric id); ready for role assignment.
    - (None, "@username") — needs an async `services.person_search.search_people` lookup by
      the caller (kept out of this function so it stays sync + DB-free, per docs/CONVENTIONS.md).
    - (None, "<human error text>") — nothing usable; caller shows this text verbatim.
    """
    # Bot API 7.0 (январь 2024) убрал forward_from/forward_date/forward_sender_name из Message
    # и заменил их одним полем forward_origin. Телеграм больше НЕ присылает старые поля — код,
    # который смотрел только на них, видел None у любой пересылки, проваливался в разбор текста
    # и отвечал «Не понял, кого добавить». Тесты этого не ловили: фейковые сообщения ставили
    # forward_from вручную. Поэтому forward_origin проверяется ПЕРВЫМ.
    origin = getattr(message, "forward_origin", None)
    if origin is not None:
        sender = getattr(origin, "sender_user", None)  # MessageOriginUser — единственный
        if sender is not None:                          # вариант, где есть настоящий telegram_id
            return sender.id, None
        # MessageOriginHiddenUser (приватность «скрывать аккаунт при пересылке»),
        # MessageOriginChat / MessageOriginChannel (переслали из чата или канала, а не от
        # человека) — id человека в таких апдейтах отсутствует физически.
        return None, (
            "В этой пересылке нет аккаунта человека — либо у него включена приватность "
            "пересылок, либо сообщение переслано из чата/канала. Попросите его @username "
            "или числовой id."
        )

    # Легаси-ветка для старых апдейтов (и для тестов, писавших forward_from напрямую).
    forwarded = getattr(message, "forward_from", None)
    if forwarded is not None:
        return forwarded.id, None
    if getattr(message, "forward_sender_name", None) or getattr(message, "forward_date", None):
        return None, (
            "У этого человека скрыт аккаунт при пересылке — попросите его @username или "
            "числовой id."
        )

    body = (message.text or "").strip()
    if not body:
        return None, _STAFF_INPUT_ERROR
    if body.startswith("@"):
        return None, body  # marker: caller resolves via services.person_search.search_people
    if body.isascii() and body.isdigit():  # same unicode-digit guard as _parse_coins_amount
        return int(body), None
    return None, _STAFF_INPUT_ERROR


def _parse_staff_role_callback(data: str) -> tuple[int | None, str | None]:
    """'roles_addrole:900802:reg_manager' -> (900802, 'reg_manager'); malformed -> (None, None).
    Same `_parse_*`-style as `_parse_appr` — never raises on crooked callback_data."""
    parts = data.split(":")
    if len(parts) != 3:
        return None, None
    _, tid_str, role = parts
    if not (tid_str.isascii() and tid_str.isdigit()):
        return None, None
    return int(tid_str), role


# ── Phase 09.1 (C, ROLE-03): manager <-> city binding step ─────────────────────────────────

async def _roles_city_kb(tid: int) -> InlineKeyboardMarkup:
    """«🌍 Все города» + one row per known city (disabled ones marked ❌, same suffix shape as
    `admin_city_switch`), plus a cancel-back-to-roles row. Shown after assigning a role (the
    add-manager flow) and from the per-person «🏙» edit button on the roles screen."""
    buttons = [[InlineKeyboardButton(text="🌍 Все города", callback_data=f"roles_city_pick:{tid}:all")]]
    for c in CITIES:
        code = c["code"]
        label = await city_label(code)
        enabled = await is_city_enabled(code)
        suffix = "" if enabled else " ❌"
        buttons.append([InlineKeyboardButton(
            text=f"{label}{suffix}", callback_data=f"roles_city_pick:{tid}:{code}",
        )])
    buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="admin_roles")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


_CITY_BINDING_SUPERADMIN_ONLY_ALERT = "Привязку к городу меняет только суперадмин"


@router.callback_query(F.data.startswith("roles_city:"))
async def roles_city_start(callback: types.CallbackQuery):
    if not await cities_module_on():
        await callback.answer()
        return
    # WR-02 (09.1-REVIEW.md): admin_city_switch already promises the human "менять может
    # суперадмин" -- this handler is the entry point that opens the picker, so the gate goes
    # here first (before rendering a screen that would refuse anyway, per CLAUDE.md).
    is_superadmin = callback.from_user.id in config.ADMIN_IDS
    if not is_superadmin:
        await callback.answer(_CITY_BINDING_SUPERADMIN_ONLY_ALERT, show_alert=True)
        return
    parts = callback.data.split(":")
    if len(parts) != 2 or not (parts[1].isascii() and parts[1].isdigit()):
        await callback.answer("Неизвестная кнопка", show_alert=True)
        return
    tid = int(parts[1])
    text = (
        "🏙 Из какого города этот менеджер? Он будет видеть заявки, чеки и гейму "
        "только своего города."
    )
    await callback.message.edit_text(text, reply_markup=await _roles_city_kb(tid))
    await callback.answer()


@router.callback_query(F.data.startswith("roles_city_pick:"))
async def roles_city_pick(callback: types.CallbackQuery):
    # WR-02 (09.1-REVIEW.md): the guarantee admin_city_switch makes the human ("менять может
    # суперадмин") was enforced only in cities.py's own set_admin_city/admin_selected_city --
    # this handler itself was gated by nothing but the `settings` capability, so any holder of
    # that capability could rebind ANY person's city, including their own, and thereby unlock
    # every other city's queues. Positive-form idiom, byte-identical to admin_city_switch:1927.
    is_superadmin = callback.from_user.id in config.ADMIN_IDS
    if not is_superadmin:
        await callback.answer(_CITY_BINDING_SUPERADMIN_ONLY_ALERT, show_alert=True)
        return
    parts = callback.data.split(":")
    if len(parts) != 3 or not (parts[1].isascii() and parts[1].isdigit()):
        await callback.answer("Неизвестная кнопка", show_alert=True)
        return
    tid = int(parts[1])
    code = parts[2]
    if code == "all":
        ok = await set_staff_city(tid, None)
        toast = "Город: все города"
    elif code in city_codes():
        ok = await set_staff_city(tid, code)
        toast = f"Город: {await city_label(code)}"
    else:
        await callback.answer("Неизвестный город", show_alert=True)
        return

    # WR-02: set_staff_city returns False when there is no staff row for tid (person removed
    # meanwhile / never staff) -- the handler used to discard the return value and toast
    # success anyway, lying to the human about a permission change actually happening.
    if not ok:
        await callback.answer("Этого менеджера уже нет в списке — обновите экран", show_alert=True)
        return

    await callback.answer(toast, show_alert=True)
    text = await render_roles_text()
    kb = await build_roles_keyboard(callback.from_user.id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data == "roles_add")
async def roles_add_start(callback: types.CallbackQuery, state: FSMContext):
    text = (
        "👥 Кого добавить менеджером?\n\n"
        "Пришлите одним сообщением:\n"
        "• пересланное сообщение от этого человека,\n"
        "• его @username,\n"
        "• или числовой telegram id."
    )
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_roles")],
    ])
    await callback.message.edit_text(text, reply_markup=cancel_kb)
    await state.set_state(StaffAdd.waiting_for_person)
    await callback.answer()


@router.message(StaffAdd.waiting_for_person, F.text.in_({"Отмена", "/cancel"}))
async def roles_add_cancel(message: types.Message, state: FSMContext):
    await state.clear()
    text = await render_roles_text()
    kb = await build_roles_keyboard(message.from_user.id)
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


async def _render_role_assign_screen(telegram_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Экран «Кого назначить» — общий рендер для мастера (после текстового ввода,
    `roles_add_person`) и прямого входа с карточки `/find` для человека, который нажимал
    /start, но анкету не подал (Phase 33, задача 2, `roles_add_for`, `handlers/admin.py::
    cmd_find_user`), — уже известным `telegram_id`, без повторного текстового ввода."""
    # Имя + @username: при одинаковых ФИО видно, того ли человека нашли.
    note = ""
    if await get_user(telegram_id) is None and await get_reg_started_by_id(telegram_id):
        note = "\n\n<i>Нажимал(а) /start, анкету пока не подавал(а).</i>"
    display_name = html_module.escape(await person_label(telegram_id))

    buttons = [
        [InlineKeyboardButton(text=meta["label"], callback_data=f"roles_addrole:{telegram_id}:{role}")]
        for role, meta in ROLES.items()
    ]
    buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="admin_roles")])
    text = f"Кого назначить: {display_name}{note}"
    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


@router.message(StaffAdd.waiting_for_person)
async def roles_add_person(message: types.Message, state: FSMContext):
    telegram_id, marker = _resolve_staff_input(message)
    if telegram_id is None and marker is not None and marker.startswith("@"):
        found = await search_people(marker, include_started=True)  # users, потом reg_started
        if not found:
            await message.answer(f"Пользователь {html_module.escape(marker)} не найден в базе бота — попросите числовой id.")
            return
        telegram_id = found[0]["user_id"]
        marker = None

    if telegram_id is None:
        await message.answer(marker or _STAFF_INPUT_ERROR)
        return

    await state.clear()
    text, kb = await _render_role_assign_screen(telegram_id)
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith("roles_addfor:"))
async def roles_add_for(callback: types.CallbackQuery):
    """Phase 33 (delegate-card admin actions, задача 2): «👥 Выдать роль» с карточки /find для
    человека, найденного ТОЛЬКО в reg_started (users его не знает — анкету не подавал) —
    прямой вход в мастер выдачи роли БЕЗ повторного текстового ввода, тот же экран, что у
    `roles_add_person` (`_render_role_assign_screen`)."""
    parts = callback.data.split(":")
    if len(parts) != 2 or not (parts[1].isascii() and parts[1].isdigit()):
        await callback.answer("Неизвестная кнопка", show_alert=True)
        return
    tid = int(parts[1])
    text, kb = await _render_role_assign_screen(tid)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("roles_addrole:"))
async def roles_assign(callback: types.CallbackQuery, bot: Bot):
    tid, role = _parse_staff_role_callback(callback.data)
    if tid is None or role not in ROLES:
        await callback.answer("Неизвестная роль", show_alert=True)
        return

    created = await add_staff(tid, role, callback.from_user.id)
    await callback.answer("Добавлен" if created else "Уже был в этой роли", show_alert=True)

    # Форум-ночь B3 (идея №22): человеку, только что впервые получившему право «checkin»
    # (сама роль его несёт СЕЙЧАС), — шпаргалка волонтёра; в канун/день форума — с отметкой
    # «отправлено» (services/checkin_volunteer_broadcast.py::greet_new_holder). `created` —
    # только на НОВОЕ назначение (повторное «Уже был в этой роли» шпаргалку не шлёт).
    if created and await has_capability(tid, "checkin"):
        await greet_new_holder(bot, tid)

    # WR-02: same superadmin-only gate as roles_city_start/roles_city_pick -- a non-superadmin
    # settings holder lands straight on the roster instead of a picker that would refuse them.
    is_superadmin = callback.from_user.id in config.ADMIN_IDS
    if is_superadmin and await cities_module_on():  # Phase 09.1 (C, ROLE-03): city step
        text = (
            "🏙 Из какого города этот менеджер? Он будет видеть заявки, чеки и гейму "
            "только своего города."
        )
        kb = await _roles_city_kb(tid)
        # Идея №6 бэклога чек-ина: «⏳ Срок» доступен сразу при выдаче — вставлена ПЕРЕД
        # «❌ Отмена» (последней строкой _roles_city_kb), не в хвост целиком.
        kb.inline_keyboard.insert(-1, [InlineKeyboardButton(
            text="⏳ Срок действия роли", callback_data=f"rexp:{tid}:{role}",
        )])
        await callback.message.edit_text(text, reply_markup=kb)
        return

    text = await render_roles_text()
    kb = await build_roles_keyboard(callback.from_user.id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith("roles_del:"))
async def roles_remove(callback: types.CallbackQuery):
    tid, role = _parse_staff_role_callback(callback.data)
    if tid is None or role not in ROLES:
        await callback.answer("Неизвестная роль", show_alert=True)
        return
    if tid in config.ADMIN_IDS:  # D-12: bootstrap superadmin, never revocable from the bot
        await callback.answer("Это суперадмин из .env, снять из бота нельзя", show_alert=True)
        return

    caps = _known_caps(await get_setting_typed(role_caps_key(role)))
    access = ", ".join(CAP_LABELS.get(c, c) for c in caps) if caps else "ничему — у этой роли сейчас нет прав"
    name = html_module.escape(await person_label(tid))
    label = ROLES[role]["label"]
    await callback.message.edit_text(
        f"Снять роль «{label}» с {name}?\n\n"
        f"Он потеряет доступ к: {access}.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Да, снять", callback_data=f"roles_del_ok:{tid}:{role}"),
            InlineKeyboardButton(text="Отмена", callback_data="admin_roles"),
        ]]),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("roles_del_ok:"))
async def roles_remove_yes(callback: types.CallbackQuery):
    tid, role = _parse_staff_role_callback(callback.data)
    if tid is None or role not in ROLES:
        await callback.answer("Неизвестная роль", show_alert=True)
        return
    if tid in config.ADMIN_IDS:
        await callback.answer("Это суперадмин из .env, снять из бота нельзя", show_alert=True)
        return

    await remove_staff(tid, role)
    text = await render_roles_text()
    kb = await build_roles_keyboard(callback.from_user.id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


# ── Идея №6 бэклога чек-ина: «⏳ Срок действия роли» ────────────────────────────────────────
# Один экран, две точки входа: кнопка «⏳ Срок действия роли» на city-picker сразу после
# `roles_assign` (свежая выдача) и «⏳ <статус>» на каждой строке `build_roles_keyboard` (правка
# уже выданной роли) — оба ведут в `rexp:<tid>:<role>`, оба заканчиваются одинаково: редрисовкой
# ростера. Не цепляется в `roles_city_pick` (та развилка обслуживает ДВА разных сценария —
# свежую выдачу и самостоятельную перепривязку города с ростера — трогать её означало бы
# протаскивать через неё лишний параметр «откуда пришли» ради одного из двух путей).

async def _resolve_forum_city_for_expiry(tid: int) -> str | None:
    """Город, для которого считать «до конца форума» (кнопка `rexp_go:*:forum`): своя
    привязка человека, если есть; при выключенном модуле городов — единственный дефолтный
    город; при включённом модуле без привязки — None (нет одного города, кнопка прячется,
    CLAUDE.md: не предлагать то, что заведомо откажет)."""
    if not await cities_module_on():
        return default_city_code()
    return await get_staff_city(tid)


async def _rexp_text_kb(tid: int, role: str) -> tuple[str, InlineKeyboardMarkup]:
    row = next((r for r in await list_staff() if r["telegram_id"] == tid and r["role"] == role), None)
    if row is None:
        return "Эта роль уже снята — обновите экран.", InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="← К ролям", callback_data="admin_roles")]]
        )
    role_label = ROLES.get(role, {}).get("label", role)
    user = await get_user(tid)
    name = html_module.escape(str((user.get("full_name") or user.get("username")) if user else tid))
    expires_at = row.get("expires_at")

    forum_city = await _resolve_forum_city_for_expiry(tid)
    forum_end = await forum_end_date_iso(forum_city) if forum_city else None

    lines = [
        f"⏳ <b>Срок действия роли</b>",
        f"{name} — {role_label}",
        "",
        f"Сейчас: <b>{_expiry_line_text(expires_at)}</b>",
    ]
    buttons = [
        [InlineKeyboardButton(text="♾ Бессрочно", callback_data=f"rexp_go:{tid}:{role}:none")],
    ]
    if forum_end:
        buttons.append([InlineKeyboardButton(
            text=f"🏁 До конца форума города ({format_ddmmyyyy(forum_end)})",
            callback_data=f"rexp_go:{tid}:{role}:forum",
        )])
    else:
        lines.append("\n<i>«До конца форума города» недоступно — не задана дата форума этого города.</i>")
    buttons.append([InlineKeyboardButton(text="✏️ Ввести дату", callback_data=f"rexp_custom:{tid}:{role}")])
    buttons.append([InlineKeyboardButton(text="← К ролям", callback_data="admin_roles")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


@router.callback_query(F.data.startswith("rexp:"))
async def roles_expiry_start(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    if len(parts) != 3 or not (parts[1].isascii() and parts[1].isdigit()) or parts[2] not in ROLES:
        await callback.answer("Неизвестная роль", show_alert=True)
        return
    tid, role = int(parts[1]), parts[2]
    text, kb = await _rexp_text_kb(tid, role)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    await callback.answer()


async def _rexp_apply_and_redraw(callback: types.CallbackQuery, tid: int, role: str, expires_at: str | None):
    ok = await set_staff_expiry(tid, role, expires_at)
    if not ok:
        await callback.answer("Этой роли уже нет — обновите экран", show_alert=True)
    else:
        await callback.answer(f"Срок: {_expiry_line_text(expires_at)}", show_alert=True)
    text = await render_roles_text()
    kb = await build_roles_keyboard(callback.from_user.id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith("rexp_go:"))
async def roles_expiry_go(callback: types.CallbackQuery):
    parts = callback.data.split(":")
    if len(parts) != 4 or not (parts[1].isascii() and parts[1].isdigit()) or parts[2] not in ROLES:
        await callback.answer("Неизвестная кнопка", show_alert=True)
        return
    tid, role, choice = int(parts[1]), parts[2], parts[3]
    if choice == "none":
        expires_at = None
    elif choice == "forum":
        forum_city = await _resolve_forum_city_for_expiry(tid)
        expires_at = await forum_end_date_iso(forum_city) if forum_city else None
        if expires_at is None:
            await callback.answer("Дата форума этого города не задана", show_alert=True)
            return
    else:
        await callback.answer("Неизвестная кнопка", show_alert=True)
        return
    await _rexp_apply_and_redraw(callback, tid, role, expires_at)


@router.callback_query(F.data.startswith("rexp_custom:"))
async def roles_expiry_custom_start(callback: types.CallbackQuery, state: FSMContext):
    parts = callback.data.split(":")
    if len(parts) != 3 or not (parts[1].isascii() and parts[1].isdigit()) or parts[2] not in ROLES:
        await callback.answer("Неизвестная роль", show_alert=True)
        return
    tid, role = int(parts[1]), parts[2]
    await state.update_data(rexp_tid=tid, rexp_role=role)
    await state.set_state(RolesExpiryEdit.waiting_date)
    await callback.message.answer(
        "До какого дня действует роль? Формат <code>ДД.ММ.ГГГГ</code>, например "
        "<code>04.10.2026</code> — роль действует ПО этот день включительно.",
        parse_mode="HTML",
        reply_markup=get_cancel_kb(),
    )
    await callback.answer()


@router.message(StateFilter(RolesExpiryEdit), Command("cancel"))
@router.message(StateFilter(RolesExpiryEdit), F.text == "Отмена")
async def roles_expiry_custom_cancel(message: types.Message, state: FSMContext):
    await state.set_state(None)
    await message.answer("Отменено.", reply_markup=ReplyKeyboardRemove())


@router.message(RolesExpiryEdit.waiting_date)
async def roles_expiry_custom_step(message: types.Message, state: FSMContext):
    data = await state.get_data()
    tid, role = data.get("rexp_tid"), data.get("rexp_role")
    await state.set_state(None)
    if tid is None or role not in ROLES:
        await message.answer("Экран устарел — откройте «👥 Роли и доступы» заново.", reply_markup=ReplyKeyboardRemove())
        return

    expires_at = parse_ddmmyyyy((message.text or "").strip())
    if expires_at is None:
        await message.answer(
            "Не понял дату. Нужен формат <code>ДД.ММ.ГГГГ</code>, например <code>04.10.2026</code> "
            "— пришлите ещё раз.",
            parse_mode="HTML",
            reply_markup=get_cancel_kb(),
        )
        await state.update_data(rexp_tid=tid, rexp_role=role)
        await state.set_state(RolesExpiryEdit.waiting_date)
        return

    ok = await set_staff_expiry(tid, role, expires_at)
    await message.answer(
        f"Срок: {_expiry_line_text(expires_at)}" if ok else "Этой роли уже нет — обновите экран.",
        reply_markup=ReplyKeyboardRemove(),
    )
    text = await render_roles_text()
    kb = await build_roles_keyboard(message.from_user.id)
    await message.answer(text, parse_mode="HTML", reply_markup=kb)
