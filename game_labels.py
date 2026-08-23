"""Phase 19 (Mini App): КОРНЕВОЙ aiogram-free модуль — переехал сюда целиком из
`handlers/game_labels.py` (там остался шим-реэкспорт). Импортировать `game_labels`, НЕ
`handlers.game_labels`: второй через пакетный `handlers/__init__.py` тянет aiogram.

Phase 16 (16-01, GAME-UI-01): единственный источник RU-подписей категорий/типов
подтверждения геймификации. Делегатский рендер (`handlers/user_actions.py`) — прямой
потребитель с этого коммита. `handlers/admin_gamification.py`'s `_proof_types_label`/
`_GAME_PROOF_LABELS` — синхронная копия для синхронных рендеров модерации
(`_render_submission_card`, чекбоксы визарда), оставлена намеренно (см. 16-03-SUMMARY).

Phase 16 (16-03, GAME-UI-03): сюда же переехал ЧИСТЫЙ рендер карточки задания
(`render_task_card_text` + `task_deadline_short`) из user_actions.py — менеджерское превью
(«👁 Как видит делегат», финальный шаг визарда) и делегатская карточка рисуются ОДНОЙ
функцией, а не двумя шаблонами, которые могут разойтись. Модуль без роутера/хендлеров.

Фейл-софт: неизвестный код категории/типа подтверждения никогда не роняет рендер —
возвращается как есть.
"""
import html
from datetime import datetime

from database.db import GAME_CATEGORIES, GAME_PROOF_TYPES, parse_proof_types, task_title
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


def task_deadline_short(task: dict) -> tuple[str, bool]:
    """(dd.mm display, is_overdue) — shared by the delegate list line, the delegate card and
    the manager preview. Quick 260819-gtl: short dd.mm date (CONTEXT.md decision 3), not the
    full dd.mm.yyyy hh:mm. Moved here verbatim from user_actions.py in 16-03."""
    try:
        dt = datetime.strptime(task["deadline_at"], "%Y-%m-%d %H:%M:%S")
        return dt.strftime("%d.%m"), dt <= datetime.now()
    except (TypeError, ValueError):
        return str(task["deadline_at"] or "—"), False


async def render_task_card_text(task: dict, status_line: str, attempt: int | None) -> str:
    """Phase 16 (16-01, GAME-UI-01): the task CARD shown when a delegate taps a task's button
    on the list -- title, RU category/coins/deadline, a composed status/attempt line, a
    proof-type hint, and the full description in a `<blockquote expandable>` (HTML-escaped
    BEFORE wrapping -- T-16-01-03). Caller truncates the RESULT for a photo caption's 1024-char
    limit (this function itself targets the no-photo/plain-message path, no ceiling of its
    own). `attempt` is accepted for interface parity with the caller (pre-composed into
    `status_line` already, not re-derived here). Accepts a real `game_tasks` row OR a
    task-shaped dict (the wizard preview builds one from FSM data before anything is saved).
    Moved here verbatim from user_actions.py in 16-03 (GAME-UI-03)."""
    title = html.escape(task_title(task))
    category = await category_label(task["category"])
    deadline, overdue = task_deadline_short(task)
    lines = [f"<b>{title}</b>", f"{category} · {task['coins']}🪙 · до {deadline}"]
    if overdue:
        # A-05 (созвон 13.08): дедлайн мягкий, сдача разрешена, решение по коинам остаётся за
        # менеджером. Phase 17.1 (17.1-01): сама формулировка — в реестре.
        lines.append(await get_setting_typed("game_task_overdue_hint_text"))
    status_label = await get_setting_typed("game_task_detail_status_label")
    lines.append(status_label.format(status=status_line))
    lines.append(f"Нужно прислать: {await proof_types_label(task.get('proof_type'))}")
    lines.append("")
    lines.append(f"<blockquote expandable>{html.escape(str(task['text']))}</blockquote>")
    return "\n".join(lines)


__all__ = [
    "category_label",
    "proof_types_label",
    "render_task_card_text",
    "task_deadline_short",
]
