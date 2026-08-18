"""Phase 16 (16-01, GAME-UI-01): единственный источник RU-подписей категорий/типов
подтверждения геймификации. Делегатский рендер (`handlers/user_actions.py`) — прямой
потребитель с этого коммита; `handlers/admin_gamification.py`'s `_proof_types_label`/
`_GAME_PROOF_LABELS` — точная копия, оставленная как есть (Plan 16-03 репойнтит её на
этот модуль вместо дублирования, не в скоупе этого плана).

Фейл-софт: неизвестный код категории/типа подтверждения никогда не роняет рендер —
возвращается как есть.
"""
from database.db import GAME_CATEGORIES, GAME_PROOF_TYPES, parse_proof_types
from settings_schema import get_setting_typed

# code (GAME_CATEGORIES) -> registry key name (game_category_label_{light,medium,hard,
# referral,special}) — один код на один ключ, порядок не важен (lookup by dict, not order).
_CATEGORY_KEY: dict[str, str] = {
    "Light": "game_category_label_light",
    "Medium": "game_category_label_medium",
    "Hard": "game_category_label_hard",
    "Referral": "game_category_label_referral",
    "Special": "game_category_label_special",
}

# Phase 17.1 (17.1-01): подписи типов подтверждения переехали из литералов в реестр —
# зеркало _CATEGORY_KEY выше (code (GAME_PROOF_TYPES) -> имя ключа game_proof_type_label_*).
# Дефолты в SETTINGS_SCHEMA байт-в-байт равны прежнему словарю PROOF_TYPE_LABELS, который
# сам был дословной копией handlers/admin_gamification.py::_GAME_PROOF_LABELS (админская
# копия остаётся литеральной до 16-03 — он репойнтит её сюда).
_PROOF_TYPE_KEY: dict[str, str] = {
    "photo": "game_proof_type_label_photo",
    "pdf": "game_proof_type_label_pdf",
    "text": "game_proof_type_label_text",
    "link": "game_proof_type_label_link",
}


async def category_label(code: str) -> str:
    """RU-подпись категории задания. Код не найден в _CATEGORY_KEY -> код без изменений."""
    key = _CATEGORY_KEY.get(code)
    if key is None:
        return code
    return await get_setting_typed(key)


async def proof_types_label(raw: str | None) -> str:
    """Пустой список типов -> `game_proof_type_unspecified_text` («не важно» по умолчанию);
    иначе RU-подписи через « + », в порядке GAME_PROOF_TYPES (не порядке ввода) —
    byte-identical к admin_gamification.py's _proof_types_label для того же входа, пока
    менеджер не переопределил подписи в настройках."""
    codes = parse_proof_types(raw)
    if not codes:
        return await get_setting_typed("game_proof_type_unspecified_text")
    labels = [await get_setting_typed(_PROOF_TYPE_KEY[c]) for c in codes]
    return " + ".join(labels)
