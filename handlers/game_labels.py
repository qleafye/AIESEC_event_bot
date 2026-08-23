"""Шим (Phase 19, Mini App): содержимое переехало в корневой `game_labels.py`, чтобы
веб-процесс мог импортировать RU-подписи и рендер карточки без aiogram (пакетный
`handlers/__init__.py` тянет aiogram при импорте любого `handlers.x`). Здесь — реэкспорт
ТЕХ ЖЕ объектов, чтобы `handlers/user_actions.py`, `handlers/admin_gamification.py`,
`handlers/admin_game_tasks.py` и тесты не правились (сторож `is` —
`tests/test_miniapp_labels_drift.py`)."""
from game_labels import *  # noqa: F401,F403
from game_labels import (  # noqa: F401 — явный реэкспорт публичных и приватных имён
    _CATEGORY_KEY,
    _PROOF_TYPE_KEY,
    category_label,
    proof_types_label,
    render_task_card_text,
    task_deadline_short,
)
