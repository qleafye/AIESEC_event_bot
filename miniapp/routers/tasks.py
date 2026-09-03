"""Phase 19 (D-07): задания делегата — список и карточка. Только чтение.

`GET /app/api/tasks` — зеркало экрана «🎯 Задания» бота (`handlers/user_actions.py`,
`_game_task_list_screen`/`_render_game_task_line`): те же аксессоры (`list_active_tasks` с
городским скоупом делегата, `get_active_submission`, `count_rejected_submissions`), те же
статусы. Дедлайн мягкий (A-05): просроченное задание остаётся в списке с `overdue: true`.
Архивные задания не отдаёт сам `list_active_tasks`.

`GET /app/api/tasks/{id}` — карточка: `card_text` рисует `game_labels.render_task_card_text`
(корневой модуль) — бот и Mini App не разъедутся. Идентичность только из `Principal`:
чужие сдачи в ответ не попадают (T-19-12).

Пагинация веб-нативная (D-07): `limit` по умолчанию 25, потолок 50, мусор -> дефолт (T-19-16).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from cities import cities_module_on, city_scope, normalize_city
from database.db import (
    count_rejected_submissions,
    get_active_submission,
    get_task,
    get_user,
    list_active_tasks,
    task_title,
)
from game_labels import category_label, proof_types_label, render_task_card_text, task_deadline_short
from settings_schema import get_setting_typed

from miniapp.deps import Principal, delegate_gate, require_section

router = APIRouter()

DEFAULT_LIMIT = 25
MAX_LIMIT = 50


def parse_page(offset, limit, *, default_limit: int = DEFAULT_LIMIT,
               max_limit: int = MAX_LIMIT) -> tuple[int, int]:
    """`(offset, limit)` из query — целые, fail-soft: мусор -> дефолт, отрицательное -> 0,
    больше потолка -> потолок."""
    try:
        off = max(0, int(offset))
    except (TypeError, ValueError):
        off = 0
    try:
        lim = int(limit)
    except (TypeError, ValueError):
        lim = default_limit
    if lim < 1:
        lim = default_limit
    return off, min(lim, max_limit)


async def delegate_city_scope(user_id: int):
    """Тот же выбор, что у бота: модуль городов выключен -> без фильтра (все задания)."""
    if not await cities_module_on():
        return None
    user = await get_user(user_id)
    return city_scope(normalize_city(user.get("event_city") if user else None))


async def submission_state(task_id: int, user_id: int) -> dict:
    """Статус задания для делегата: `new` (сдачи нет) · `pending` · `approved` · `rejected`
    (нет активной, но есть отклонённые — с номером попытки). `can_submit` — нет неотклонённой
    сдачи и не исчерпан `game_resubmit_limit` (как у бота: лимит 0/пусто = без лимита)."""
    active = await get_active_submission(task_id, user_id)
    rejected = await count_rejected_submissions(task_id, user_id)
    limit = await get_setting_typed("game_resubmit_limit") or 0
    if active is not None and active["status"] == "pending":
        status, can_submit = "pending", False
    elif active is not None and active["status"] == "approved":
        status, can_submit = "approved", False
    elif rejected > 0:
        status, can_submit = "rejected", not (limit and rejected >= limit)
    else:
        status, can_submit = "new", True
    return {
        "status": status,
        "attempt": rejected,
        "limit": limit,
        "can_submit": can_submit,
        "submitted_at": active.get("submitted_at") if active else None,
        "coins_awarded": active.get("coins_awarded") if active else None,
    }


async def _list_item(task: dict, user_id: int) -> dict:
    deadline_short, overdue = task_deadline_short(task)
    state = await submission_state(task["id"], user_id)
    return {
        "id": task["id"],
        "title": task_title(task),
        "category": task["category"],
        "category_label": await category_label(task["category"]),
        "coins": task["coins"],
        "deadline_at": task["deadline_at"],
        "deadline_short": deadline_short,
        "overdue": overdue,
        "photo_file_id": task.get("photo_file_id"),
        **state,
    }


async def tasks_progress(user_id: int, city_scope) -> tuple[int, int]:
    """`(done, total)` — сколько активных заданий делегата уже принято (`status == "approved"`)
    из общего числа активных. Общий помощник для `/app/api/hub` (план 23.1-03, факт «N из M
    заданий сдано»): тот же `list_active_tasks(city_scope=…)` + `submission_state`, что и
    список заданий выше — второго источника правды не заводим."""
    all_tasks = await list_active_tasks(city_scope=city_scope)
    done = 0
    for task in all_tasks:
        state = await submission_state(task["id"], user_id)
        if state["status"] == "approved":
            done += 1
    return done, len(all_tasks)


@router.get("/app/api/tasks")
async def tasks_list(offset: str | None = None, limit: str | None = None,
                     p: Principal = Depends(delegate_gate),
                     _: Principal = Depends(require_section("tasks"))) -> dict:
    off, lim = parse_page(offset, limit)
    all_tasks = await list_active_tasks(city_scope=await delegate_city_scope(p.telegram_id))
    page = all_tasks[off:off + lim]
    return {
        "items": [await _list_item(t, p.telegram_id) for t in page],
        "total": len(all_tasks),
        "limit": lim,
        "offset": off,
        "empty_text": await get_setting_typed("game_task_list_empty") if not all_tasks else None,
    }


@router.get("/app/api/tasks/{task_id}")
async def task_card(task_id: int, p: Principal = Depends(delegate_gate),
                    _: Principal = Depends(require_section("tasks"))) -> dict:
    task = await get_task(task_id)
    if task is None or task.get("archived_at"):
        raise HTTPException(404, {"reason": "task_not_found"})
    item = await _list_item(task, p.telegram_id)
    # Строка статуса — дословно как в боте (`mytask_open`).
    if item["status"] == "pending":
        status_line, attempt = "на проверке", None
    elif item["status"] == "approved":
        status_line, attempt = f"принято (+{item['coins_awarded']}🪙)", None
    else:
        limit, rejected = item["limit"], item["attempt"]
        status_line = f"новое · попытка {rejected} из {limit}" if limit and rejected else "новое"
        attempt = rejected
    item.update({
        "text": task["text"],
        "proof_type": task.get("proof_type"),
        "proof_hint": await proof_types_label(task.get("proof_type")),
        "status_line": status_line,
        # Готовый HTML-текст карточки (как в боте) — для паритета и тестов; фронт рисует
        # структурные поля через textContent (innerHTML запрещён, T-19-15).
        "card_text": await render_task_card_text(task, status_line, attempt),
        "overdue_hint": await get_setting_typed("game_task_overdue_hint_text") if item["overdue"] else None,
    })
    return item
