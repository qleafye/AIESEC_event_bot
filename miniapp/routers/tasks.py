"""Phase 19 (D-07): задания делегата — список и карточка. Только чтение.

`GET /app/api/tasks` — зеркало экрана «🎯 Задания» бота (`handlers/user_actions.py`,
`_game_task_list_screen`/`_render_game_task_line`): те же аксессоры (`list_active_tasks` с
городским скоупом делегата, `get_active_submission`, `count_rejected_submissions`), те же
статусы и тот же порядок (`game_labels.sort_tasks_for_delegate` — открытые первыми,
просроченные в хвосте; квик 260919-m9x). Дедлайн мягкий (A-05): просроченное задание
остаётся в списке с `overdue: true`.
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
from game_labels import (
    ambassador_block_index,
    category_label,
    proof_types_label,
    render_task_card_text,
    sort_tasks_for_ambassador,
    sort_tasks_for_delegate,
    task_deadline,  # Phase 32 (32-14): единственный разбор `deadline_at` — вместо своего strptime
    task_deadline_short,
    task_deadline_text,  # Phase 32 (32-14): готовая подпись срока делегату («без срока» из реестра)
    task_has_deadline,
    visible_tasks_for,
)
from services import i18n
from services.ambassador_waves import wave_visibility_ids  # хвост CR-03: та же пара id, что у бота
from settings_schema import get_setting_typed

from miniapp.deps import Principal, delegate_gate, require_section
from miniapp.timeutil import today_msk

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


async def _ambassador_gate(user_id: int) -> tuple[dict | None, object, bool, set[int], set[int]]:
    """Phase 32 (32-06, D-28/D-31/D-36/D-38): ОДИН `get_user` — и городской скоуп (как у
    `delegate_city_scope`), и `is_ambassador`/`ambassador_path`/доступные волны берутся из него
    же. Возвращает `(user, city_scope, is_ambassador, eligible_wave_ids, open_wave_ids)` —
    вызывающая сторона (`tasks_list`/`task_card`/`tasks_progress`) кормит их в
    `visible_tasks_for`/`sort_tasks_for_ambassador`, ТЕ ЖЕ функции, что и бот (единственное
    правило видимости на проект, T-32-06-02).

    Хвост CR-03 (32-FIX-common-2): раньше `open_wave_ids` не считался вовсе (только
    `eligible_wave_ids`, и только для амбассадора) — задание черновой/будущей/уже закрытой
    волны с `audience="all"` было видно и обычному делегату (`visible_tasks_for` без
    `open_wave_ids` не сужает такие задания). `wave_visibility_ids` — тот же аксессор, что
    зовёт `handlers/user_actions.py::_game_task_list_screen`, теперь Mini App читает волны им
    же, а не собирает вторую копию правила."""
    user = await get_user(user_id)
    scope = city_scope(normalize_city(user.get("event_city") if user else None)) if await cities_module_on() else None
    is_ambassador = bool(user and user.get("is_ambassador"))
    wave_ids, open_ids = await wave_visibility_ids(user, city_scope=scope)
    return user, scope, is_ambassador, wave_ids, open_ids


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


async def _list_item(task: dict, user_id: int, lang: str = "ru", tr_map: dict | None = None) -> dict:
    tr_map = tr_map or {}
    deadline_short, overdue = task_deadline_short(task)
    state = await submission_state(task["id"], user_id)
    return {
        "id": task["id"],
        "title": task_title(task),
        "category": task["category"],
        "category_label": i18n.tr(await category_label(task["category"]), lang, tr_map),
        "coins": task["coins"],
        "deadline_at": task["deadline_at"],
        "deadline_short": deadline_short,
        # Phase 32 (32-14, D-27): признак наличия срока + готовая делегатская подпись
        # (короткая дата ИЛИ «без срока» из реестра, тем же путём перевода, что category_label
        # выше) — фронт больше не собирает «до …» сам и не видит служебную метку.
        "has_deadline": task_has_deadline(task),
        "deadline_text": i18n.tr(await task_deadline_text(task), lang, tr_map),
        "overdue": overdue,
        "photo_file_id": task.get("photo_file_id"),
        **state,
    }


async def tasks_progress(user_id: int, city_scope) -> tuple[int, int]:
    """`(done, total)` — сколько активных заданий делегата уже принято (`status == "approved"`)
    из общего числа активных. Общий помощник для `/app/api/hub` (план 23.1-03, факт «N из M
    заданий сдано»): тот же `list_active_tasks(city_scope=…)` + `submission_state`, что и
    список заданий выше — второго источника правды не заводим.

    Rule 1 (32-06, D-28/D-36): без фильтра «N из M» считало бы амбассадорские задания в
    знаменателе для ОБЫЧНОГО делегата — плита хаба показала бы завышенный total, которого он
    физически не может закрыть (задания ему не видны вовсе)."""
    _user, _scope, is_ambassador, wave_ids, open_ids = await _ambassador_gate(user_id)
    all_tasks = await list_active_tasks(city_scope=city_scope)
    all_tasks = visible_tasks_for(
        all_tasks, is_ambassador=is_ambassador, eligible_wave_ids=wave_ids, open_wave_ids=open_ids,
    )
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
    lang, tr_map = await i18n.context(p.telegram_id)
    lang = lang if lang in ("ru", "en") else "ru"
    user, scope, is_ambassador, wave_ids, open_ids = await _ambassador_gate(p.telegram_id)
    all_tasks = await list_active_tasks(city_scope=scope)
    # Phase 32 (32-06, D-28/D-31/D-36/D-38): то же правило видимости, что у бота — ДО сортировки
    # (T-32-06-02: вторая забытая копия — дыра, поэтому структурный сторож ниже проверяет, что
    # рядом с `list_active_tasks` в этом файле всегда есть `visible_tasks_for`). Хвост CR-03:
    # `open_wave_ids` сужает задание `audience="all"` черновой/будущей волны для НЕ-амбассадора
    # тоже — раньше сюда передавался только `eligible_wave_ids`.
    all_tasks = visible_tasks_for(
        all_tasks, is_ambassador=is_ambassador, eligible_wave_ids=wave_ids, open_wave_ids=open_ids,
    )
    # Квик 260919-m9x: тот же порядок, что у списка бота (`sort_tasks_for_delegate`) —
    # открытые задания первыми, просроченные в хвосте; пагинация режет уже отсортированное.
    all_tasks = sort_tasks_for_delegate(all_tasks)
    # Phase 32 (32-06, D-24/D-28): амбассадорский блок наверх + порядок по пути — тот же второй
    # пересорт, что у бота.
    all_tasks = sort_tasks_for_ambassador(
        all_tasks, is_ambassador=is_ambassador, path=user.get("ambassador_path") if user else None,
    )
    page = all_tasks[off:off + lim]
    return {
        "items": [await _list_item(t, p.telegram_id, lang, tr_map) for t in page],
        "total": len(all_tasks),
        "limit": lim,
        "offset": off,
        "empty_text": (
            await i18n.tr_setting("game_task_list_empty", lang, tr_map) if not all_tasks else None
        ),
    }


def _deadline_days_left(task: dict, overdue: bool) -> int | None:
    """Целое число полных дней от московского «сегодня» (`miniapp.timeutil.today_msk`) до
    срока задания. Phase 32 (32-14, D-27): дата берётся через общий `game_labels.task_deadline`
    (метка «конца времён» отдаёт `None`, значит у задания без срока строки «осталось N дней»
    не будет вовсе — вместо обратного отсчёта на ~2,9 млн дней). Просроченное задание уже несёт
    `overdue_hint` — вторую строку не дублируем; дедлайна нет или дата не разбирается -> `None`."""
    if overdue:
        return None
    target_dt = task_deadline(task)
    if target_dt is None:
        return None
    delta = (target_dt.date() - today_msk()).days
    return delta if delta >= 0 else None


@router.get("/app/api/tasks/{task_id}")
async def task_card(task_id: int, p: Principal = Depends(delegate_gate),
                    _: Principal = Depends(require_section("tasks"))) -> dict:
    task = await get_task(task_id)
    if task is None or task.get("archived_at"):
        raise HTTPException(404, {"reason": "task_not_found"})
    # Phase 32 (32-06, D-28/D-36, T-32-06-01/02): прямой запрос по id обходит список — карточка
    # перепроверяет видимость САМА (`visible_tasks_for` на списке из одного задания), иначе
    # амбассадорская ссылка/чужая волна утекли бы не-амбассадору, знающему id.
    _user_for_gate, _scope, _is_amb, _wave_ids, _open_ids = await _ambassador_gate(p.telegram_id)
    if not visible_tasks_for(
        [task], is_ambassador=_is_amb, eligible_wave_ids=_wave_ids, open_wave_ids=_open_ids,
    ):
        raise HTTPException(404, {"reason": "task_not_found"})
    lang, tr_map = await i18n.context(p.telegram_id)
    lang = lang if lang in ("ru", "en") else "ru"
    item = await _list_item(task, p.telegram_id, lang, tr_map)
    # Строка статуса — дословно как в боте (`mytask_open`) на РУССКОМ (card_text — общий с
    # ботом рендер, из этой задачи не переводится, T-19-15 держит JS на структурных полях, не
    # на card_text). Структурное поле `status_line` ниже переводится ОТДЕЛЬНО от card_text:
    # шаблон переводится ДО подстановки чисел (tr() по уже готовой строке "принято (+3🪙)" не
    # найдёт перевод — у каждого числа своя строка, у шаблона одна).
    if item["status"] == "pending":
        status_line, attempt = "на проверке", None
        status_line_tpl = "на проверке"
    elif item["status"] == "approved":
        status_line, attempt = f"принято (+{item['coins_awarded']}🪙)", None
        status_line_tpl = i18n.tr("принято (+{n}🪙)", lang, tr_map).format(n=item["coins_awarded"])
    else:
        limit, rejected = item["limit"], item["attempt"]
        if limit and rejected:
            status_line = f"новое · попытка {rejected} из {limit}"
            status_line_tpl = i18n.tr("новое · попытка {n} из {limit}", lang, tr_map).format(
                n=rejected, limit=limit,
            )
        else:
            status_line = "новое"
            status_line_tpl = "новое"
        attempt = rejected

    # Phase 23.1-05 (UI-REDESIGN-06): плита с наградой и остатком срока, блок «нужно
    # прислать», строки фактов (макет 05-task.png) — все подписи из реестра, числа
    # подставляются здесь (D-06).
    deadline_left_tpl = await i18n.tr_setting("miniapp_task_deadline_left_text", lang, tr_map)
    days_left = _deadline_days_left(task, item["overdue"])
    deadline_left_text = (
        deadline_left_tpl.format(days=days_left) if (days_left is not None and deadline_left_tpl) else None
    )

    item.update({
        "text": task["text"],
        "proof_type": task.get("proof_type"),
        "proof_hint": i18n.tr(await proof_types_label(task.get("proof_type")), lang, tr_map),
        "status_line": status_line_tpl,
        # Готовый HTML-текст карточки (как в боте) — для паритета и тестов; фронт рисует
        # структурные поля через textContent (innerHTML запрещён, T-19-15).
        "card_text": await render_task_card_text(task, status_line, attempt),
        "overdue_hint": (
            await i18n.tr_setting("game_task_overdue_hint_text", lang, tr_map) if item["overdue"] else None
        ),
        "deadline_left_text": deadline_left_text,
        "todo_eyebrow": await i18n.tr_setting("miniapp_task_todo_eyebrow", lang, tr_map),
        "proof_eyebrow": await i18n.tr_setting("miniapp_task_proof_eyebrow", lang, tr_map),
        "proof_note": await i18n.tr_setting("miniapp_task_proof_note", lang, tr_map),
        "review_note": await i18n.tr_setting("miniapp_task_review_note", lang, tr_map),
    })
    return item
