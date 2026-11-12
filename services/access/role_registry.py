"""Реестр ролей: словарь ROLES и ключи настроек role_caps_*/role_*_enabled.

Содержимое и формат ключей — как были в handlers.access.admin_caps.
Хендлер реэкспортирует те же имена.
"""
from __future__ import annotations

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
    # приглашённый ссылкой (`handlers/forum/admin_volunteer_invite.py`) или добавленный вручную тем
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
