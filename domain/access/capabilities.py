"""Права и роли админки без aiogram.

Реестр настроек и Mini App читают этот модуль. Список живёт здесь, а не в
`handlers/access/admin_caps.py`: иначе первое чтение дефолта роли тянет роутеры бота.
"""

ALL_CAPABILITIES = [
    "moderate_reg",
    "moderate_receipts",
    "moderate_game",
    "broadcast",
    "settings",
    "stats",
    "checkin",
    # Одобрять человека у стойки — отдельно от отметки входа.
    "checkin_approve",
    # Ссылки с метками и их статистика. Само по себе экраны делегатов не открывает.
    "source_links",
]


def role_caps_key(role: str) -> str:
    return f"role_caps_{role}"


def role_enabled_key(role: str) -> str:
    return f"role_{role}_enabled"


# «admin» здесь нет: это config.ADMIN_IDS, из бота не отзывается.
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
    "volunteer": {
        "label": "🎗 Волонтёр форума",
        "default_caps": ["checkin"],
    },
    "reg_volunteer": {
        "label": "🎗 Волонтёр регистрации",
        "default_caps": ["checkin", "checkin_approve"],
    },
    "marketing_manager": {
        "label": "📣 Маркетинг (метки)",
        "default_caps": ["source_links"],
    },
    "owner": {
        "label": "👑 Второй владелец",
        "default_caps": list(ALL_CAPABILITIES),
    },
}
