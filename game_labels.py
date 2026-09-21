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

Phase 32 (32-04, D-25/D-27): сюда же — показ задания без срока делегату («без срока», а не
служебная метка «конца времён») и штраф за просрочку (одна формула на проект, строка-подсказка
на карточке ДО сдачи). Менеджерским экранам — свой синхронный `task_deadline_admin` (без
реестра, формат параметром): перевод существующих читателей на него — планы 32-06, 32-07 и
32-14 (см. «Карту читателей» в 32-04-PLAN.md), не этот план.
"""
import html
from datetime import datetime

from database.db import GAME_CATEGORIES, GAME_PROOF_TYPES, NO_DEADLINE_AT, parse_proof_types, task_title
from services.timeutil import msk_now
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


def task_deadline(task: dict) -> datetime | None:
    """`deadline_at` строкой -> naive-датой по Москве, как её и вводил менеджер, или `None`
    (дедлайна нет / строку не разобрать). Единственный разбор этого поля в модуле —
    `task_deadline_short` и `sort_tasks_for_delegate` зовут его, а не strptime по копии.

    Phase 32 (32-04, D-27): `deadline_at`, равная служебной метке `NO_DEADLINE_AT` (задание
    без собственного дедлайна), тоже даёт `None` — так задание «без срока» никогда не
    считается просроченным и автоматически встаёт в хвост открытых у `sort_tasks_for_delegate`
    (группа 0, ключ `_NO_DEADLINE` = `datetime.max`, дальше всех настоящих дат)."""
    if task.get("deadline_at") == NO_DEADLINE_AT:
        return None
    try:
        return datetime.strptime(task["deadline_at"], "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, KeyError):
        return None


def task_has_deadline(task: dict) -> bool:
    """Phase 32 (32-04, D-27): единственный предикат «у задания есть срок» на весь проект —
    напоминания (план 32-08), штраф (`penalty_hint_line` ниже, план 32-07) и визард (план
    32-12) зовут его, а не сравнивают `deadline_at` со строкой сами."""
    return task_deadline(task) is not None


def task_deadline_short(task: dict) -> tuple[str, bool]:
    """(dd.mm display, is_overdue) — shared by the delegate list line, the delegate card and
    the manager preview. Quick 260819-gtl: short dd.mm date (CONTEXT.md decision 3), not the
    full dd.mm.yyyy hh:mm. Moved here verbatim from user_actions.py in 16-03.

    Квик 260919-m9x: «сейчас» — московское (`services.timeutil.msk_now`), а не
    `datetime.now()`. Дедлайн менеджер вводит по Москве, а контейнер на проде живёт в UTC:
    задание «до 23:59» считалось открытым ещё три часа, до 02:59 МСК следующих суток —
    расходилось и со строкой «срок вышел», и с обратным отсчётом дней в приложении, который
    уже считался по Москве (`miniapp/routers/tasks.py::_deadline_days_left`).

    Phase 32 (32-04, D-27): задание без срока (метка `NO_DEADLINE_AT`) даёт `("", False)` —
    пустую строку, не сырую служебную метку; поведение для разбираемой даты и для мусора
    (строка есть, но не парсится, или поля нет вовсе) не меняется."""
    if task.get("deadline_at") == NO_DEADLINE_AT:
        return "", False
    dt = task_deadline(task)
    if dt is None:
        return str(task.get("deadline_at") or "—"), False
    return dt.strftime("%d.%m"), dt <= msk_now()


async def task_deadline_text(task: dict) -> str:
    """Phase 32 (32-04, D-27): срок ДЕЛЕГАТУ человеческими словами — короткая дата (как и
    раньше), а при отсутствии срока текст из реестра `game_task_no_deadline_text` («без
    срока» по умолчанию) вместо пустой строки/служебной метки. Единственное место, где текст
    показа срока делегату подставляется из реестра — карточка (`render_task_card_text`) и
    любой будущий делегатский экран зовут эту функцию, а не собирают строку сами."""
    short, _overdue = task_deadline_short(task)
    if short:
        return short
    return await get_setting_typed("game_task_no_deadline_text")


def task_deadline_admin(task: dict, fmt: str = "%d.%m %H:%M") -> str:
    """Phase 32 (32-04, D-27): срок МЕНЕДЖЕРУ — синхронная, реестра не читает (менеджерские
    экраны проекта собираются литералами модуля, та же граница, что у бейджа
    `handlers/game_review_render.py`). Формат приходит параметром: бот печатает
    `%d.%m %H:%M`, веб-редактор Mini App — `%d.%m.%Y %H:%M`. Срока нет -> литерал «без срока»;
    строку разобрать не удалось -> прежний фейл-софт (значение как есть, `—` для пустого) —
    byte-identical копия `_game_task_deadline_display` (`handlers/admin_gamification.py`) для
    ветки успешного разбора. Единственный помощник, которым менеджерским экранам разрешено
    печатать срок — приватные копии удаляют планы 32-07 и 32-14, дальше собственный
    `strptime` по `deadline_at` запрещён структурным сторожем плана 32-14."""
    raw = task.get("deadline_at")
    if raw == NO_DEADLINE_AT:
        return "без срока"
    try:
        return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").strftime(fmt)
    except (TypeError, ValueError):
        return str(raw or "—")


# Сортировочный «конец времён» для задания без дедлайна: такое задание не просрочено
# никогда, и в группе открытых оно должно стоять последним, а не первым.
_NO_DEADLINE = datetime.max


def sort_tasks_for_delegate(tasks: list[dict]) -> list[dict]:
    """Порядок делегатского списка заданий: сначала ОТКРЫТЫЕ (срок не вышел) — ближайший
    дедлайн выше, потом ПРОСРОЧЕННЫЕ — свежие выше. Чистая функция: ни БД, ни реестра,
    исходный список не меняется (общая для бота и Mini App, как и весь этот модуль).

    Зачем (квик 260919-m9x): `database.db.list_active_tasks` отдаёт `ORDER BY deadline_at
    ASC`, дедлайн мягкий (A-05), архивируют задания руками — поэтому наверху делегатского
    списка стояли самые старые, августовские задания, а свежее уезжало на вторую страницу
    (в боте страница — шесть штук). На проде это стоило 200 сдач, ушедших в задания с
    истёкшим дедлайном, из которых 195 менеджер отклонил руками."""
    def key(task: dict):
        dt = task_deadline(task)
        if dt is None:
            return (0, _NO_DEADLINE, task.get("id") or 0)
        if dt <= msk_now():
            # Просроченные — вторая группа; внутри неё свежие выше (обратный порядок даты).
            return (1, -dt.timestamp(), task.get("id") or 0)
        return (0, dt, task.get("id") or 0)

    # Две группы сравниваются только между собой (ключ начинается с номера группы), поэтому
    # разнотипные вторые элементы (datetime у открытых, float у просроченных) не встречаются.
    return sorted(tasks, key=key)


def penalized_coins(coins: int, percent: int) -> int:
    """Phase 32 (32-04, D-25/D-35): единственная формула штрафа за сдачу после дедлайна на
    весь проект — карточка задания (`penalty_hint_line` ниже) и одобрение сдачи (план 32-07)
    считают ОДНО и то же, тест сравнивает обе точки (T-32-04-04). `percent` вне 0..100
    приводится к границам (защита от опечатки в настройке, D-35 — «один процент на
    событие»); результат округляется ВНИЗ (`coins - coins * percent // 100`), чтобы делегат
    никогда не получил больше обещанного, и не может уйти в минус."""
    percent = max(0, min(100, percent))
    return max(0, coins - coins * percent // 100)


async def penalty_hint_line(task: dict) -> str | None:
    """Phase 32 (32-04, D-25): строка штрафа ДО сдачи — «после дедлайна — 70 баллов вместо
    100». `None`, если у задания нет срока (штраф без дедлайна бессмыслен) ИЛИ
    `game_late_penalty_percent` равен 0 — на событиях без штрафа (дефолт на каждом живом
    событии) карточка не меняется вовсе."""
    if not task_has_deadline(task):
        return None
    percent = await get_setting_typed("game_late_penalty_percent")
    if not percent:
        return None
    dt = task_deadline(task)
    coins = task["coins"]
    penalized = penalized_coins(coins, percent)
    template = await get_setting_typed("game_task_penalty_hint_text")
    return template.format(deadline=dt.strftime("%d.%m %H:%M"), penalized=penalized, coins=coins)


async def render_task_card_text(task: dict, status_line: str, attempt: int | None) -> str:
    """Phase 16 (16-01, GAME-UI-01): the task CARD shown when a delegate taps a task's button
    on the list -- title, RU category/coins/deadline, a composed status/attempt line, a
    proof-type hint, and the full description in a `<blockquote expandable>` (HTML-escaped
    BEFORE wrapping -- T-16-01-03). Caller truncates the RESULT for a photo caption's 1024-char
    limit (this function itself targets the no-photo/plain-message path, no ceiling of its
    own). `attempt` is accepted for interface parity with the caller (pre-composed into
    `status_line` already, not re-derived here). Accepts a real `game_tasks` row OR a
    task-shaped dict (the wizard preview builds one from FSM data before anything is saved).
    Moved here verbatim from user_actions.py in 16-03 (GAME-UI-03).

    Phase 32 (32-04, D-25/D-27): деталь дедлайна на второй строке получает ветку «без срока»
    (вместо «до {дата}» — просто текст `task_deadline_text`, без «до»); сразу под ней —
    строка штрафа `penalty_hint_line`, когда та не `None`. При проценте штрафа 0 и наличии
    срока вторая строка и остальной рендер байт-в-байт равны поведению до этого плана."""
    title = html.escape(task_title(task))
    category = await category_label(task["category"])
    deadline, overdue = task_deadline_short(task)
    if deadline:
        deadline_line = f"{category} · {task['coins']}🪙 · до {deadline}"
    else:
        deadline_line = f"{category} · {task['coins']}🪙 · {await task_deadline_text(task)}"
    lines = [f"<b>{title}</b>", deadline_line]
    if overdue:
        # A-05 (созвон 13.08): дедлайн мягкий, сдача разрешена, решение по коинам остаётся за
        # менеджером. Phase 17.1 (17.1-01): сама формулировка — в реестре.
        lines.append(await get_setting_typed("game_task_overdue_hint_text"))
    penalty_line = await penalty_hint_line(task)
    if penalty_line is not None:
        lines.append(penalty_line)
    status_label = await get_setting_typed("game_task_detail_status_label")
    lines.append(status_label.format(status=status_line))
    lines.append(f"Нужно прислать: {await proof_types_label(task.get('proof_type'))}")
    lines.append("")
    lines.append(f"<blockquote expandable>{html.escape(str(task['text']))}</blockquote>")
    return "\n".join(lines)


__all__ = [
    "category_label",
    "penalized_coins",
    "penalty_hint_line",
    "proof_types_label",
    "render_task_card_text",
    "sort_tasks_for_delegate",
    "task_deadline",
    "task_deadline_admin",
    "task_deadline_short",
    "task_deadline_text",
    "task_has_deadline",
]
