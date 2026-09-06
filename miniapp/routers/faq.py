"""Quick 260906-8uq (FAQ-01..06): раздел «❓ Частые вопросы» в Mini App.

`GET /app/api/faq` (задача 5) — делегатский список. Правило видимости пункта («городской
пункт перекрывает общий») живёт ОДИН раз в `services/faq.py::apply_city_overrides` — здесь
второй копии нет, только `list_faq_for_city` + применение правила (тот же приём, что
`handlers/user_actions.py::_faq_visible_items` использует на стороне бота).

Город делегата: `Principal.city` — привязка СОТРУДНИКА (`staff_city`), у делегата она всегда
`None`, поэтому город читаем из его собственной строки `users.event_city` — тот же fail-soft
приём, что `handlers.user_actions._delegate_city_for_faq` (ошибка чтения не роняет экран,
только сужает список до общих пунктов).

`POST /app/api/faq` (задача 6) — менеджерская симметрия кнопки «❓ В FAQ» из журнала вопросов
(`handlers/admin_faq.py::afaq_save_draft`), но МИНИМАЛЬНО: тело `{question, answer}`, город —
`Principal.city` (привязка МЕНЕДЖЕРА, не делегата — другая сторона той же дихотомии, что и у
`_delegate_city` выше), пусто -> общий пункт. Валидация и дедупликация переиспользуются менед-
жерским блоком ниже (`_create_checked`) — второй копии нет.

Quick 260906-nxp: полноценный экран управления FAQ в приложении — `GET/POST /app/api/admin/faq`,
`PATCH /app/api/admin/faq/{id}`, `POST /app/api/admin/faq/{id}/move`,
`DELETE /app/api/admin/faq/{id}` — под `require_cap("moderate_reg")` + `require_section("faq")`.
Раздел `faq` теперь гейтит и делегатский список (`GET /app/api/faq`), и менеджерское ведение
(блок ниже) — второго чекбокса не заводится (см. `not_in_scope` плана 260906-nxp: новый раздел
потребовал бы правки `settings_schema.py`/`SECTION_KEYS`, это касание бота, вне worktree квика).
Город пункта в мутациях НИКОГДА не приходит из тела — только `Principal.city` (та же дихотомия,
что у делегатского города выше, только для другой стороны): PATCH принимает лишь пару
`"all"`/`"mine"`, код города фронт не знает и прислать не может (T-nxp-02). Скоуп менеджера
(`services.applications.manager_scope`) проверяется на КАЖДОЙ мутации через `_load_in_scope`
(T-nxp-03), не только на чтении — экран в вебвью живёт долго, привязка могла смениться."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from cities import cities_module_on, city_label, normalize_city
from database.db import (
    create_faq_item,
    delete_faq_item,
    get_faq_item,
    get_user,
    list_faq_for_city,
    list_faq_items,
    reorder_faq_items,
    update_faq_item,
)
from services import applications
from services.faq import apply_city_overrides, city_badge, normalize_question
from settings_schema import get_setting_typed

from miniapp.deps import Principal, delegate_gate, require_cap, require_section

router = APIRouter()

QUESTION_MAX = 300
ANSWER_MAX = 4000

EMPTY_QUESTION_TEXT = "Вопрос не может быть пустым — напишите текст вопроса."
EMPTY_ANSWER_TEXT = "Ответ не может быть пустым — напишите текст ответа."
QUESTION_TOO_LONG_TEXT = f"Вопрос длиннее {QUESTION_MAX} символов — сократите текст."
ANSWER_TOO_LONG_TEXT = f"Ответ длиннее {ANSWER_MAX} символов — сократите текст."


async def _delegate_city(p: Principal) -> str | None:
    if not await cities_module_on():
        return None
    try:
        user = await get_user(p.telegram_id)
        return normalize_city(user.get("event_city") if user else None)
    except Exception:
        return None


@router.get("/app/api/faq")
async def faq_list(
    p: Principal = Depends(delegate_gate),
    _: Principal = Depends(require_section("faq")),
) -> dict:
    city = await _delegate_city(p)
    try:
        rows = await list_faq_for_city(city)
    except Exception:
        rows = []
    items = apply_city_overrides(rows, city)
    return {
        "items": [
            {"id": row["id"], "question": row["question"], "answer": row["answer"]}
            for row in items
        ],
        "empty_text": await get_setting_typed("faq_empty_text"),
    }


class FaqIn(BaseModel):
    question: str = ""
    answer: str = ""


async def _create_checked(*, question: str, answer: str, city: str | None, created_by: int) -> dict:
    """Общая проверка + дедупликация создания пункта — переиспользуется тонкой обёрткой
    `POST /app/api/faq` (журнал вопросов, задача 6) и менеджерским `POST /app/api/admin/faq`
    (задача 1 квика 260906-nxp). Дубль ищем СРЕДИ пунктов ТОГО ЖЕ городского ведра (точное
    совпадение `city == target_city`), не через `apply_city_overrides` — та функция про
    делегатскую видимость, не про поиск дублей (та же дедупликация, что `afaq_save_draft` в
    боте)."""
    question = question.strip()
    answer = answer.strip()
    if not question:
        raise HTTPException(400, {"reason": "empty", "text": EMPTY_QUESTION_TEXT})
    if not answer:
        raise HTTPException(400, {"reason": "empty", "text": EMPTY_ANSWER_TEXT})
    if len(question) > QUESTION_MAX:
        raise HTTPException(400, {"reason": "too_long", "text": QUESTION_TOO_LONG_TEXT})
    if len(answer) > ANSWER_MAX:
        raise HTTPException(400, {"reason": "too_long", "text": ANSWER_TOO_LONG_TEXT})

    all_items = await list_faq_items(city_scope=None)
    norm_target = normalize_question(question)
    for row in all_items:
        if row.get("city") != city:
            continue
        if normalize_question(row.get("question")) == norm_target:
            return {"ok": False, "reason": "already", "id": row["id"]}

    new_id = await create_faq_item(city=city, question=question, answer=answer, created_by=created_by)
    return {"ok": True, "id": new_id}


@router.post("/app/api/faq")
async def faq_create(
    body: FaqIn,
    p: Principal = Depends(require_cap("moderate_reg")),
    _: Principal = Depends(require_section("questions")),
) -> dict:
    return await _create_checked(
        question=body.question, answer=body.answer, city=p.city or None, created_by=p.telegram_id,
    )


# ══════════════════════════════════════════════════════════════════════════════════════════
# Quick 260906-nxp: менеджерский экран #/admin-faq — список/создание/правка/порядок/удаление
# ══════════════════════════════════════════════════════════════════════════════════════════

ADMIN_LIMIT_DEFAULT = 25
ADMIN_LIMIT_MAX = 50

ADMIN_EMPTY_TEXT = "Пока ни одного пункта — добавьте первый кнопкой «Добавить»."
CITY_HINT_TEXT = (
    "Город можно переключить только менеджеру, привязанному к конкретному городу — ваша "
    "учётка не привязана, пункт останется для всех городов."
)
TEXT_ONE_FIELD = "За один раз меняется одно поле — вопрос, ответ, видимость или город."
TEXT_BAD_CITY = "Город — только «для всех» или «мой город»."
TEXT_NO_CITY_BINDING = (
    "Ваша учётка не привязана к городу — пункт можно оставить только для всех городов."
)
TEXT_BAD_DIRECTION = "Направление — только вверх или вниз."
TEXT_OUT_OF_SCOPE = "Это пункт другого города — его ведёт менеджер того города."
TEXT_NOT_FOUND = "Пункт не найден — возможно, его уже удалили."


def _parse_admin_int(raw, default: int, lo: int, hi: int) -> int:
    """Аналог `miniapp/routers/questions.py::_parse_int` — мусор в query = дефолт, не 400."""
    try:
        return max(lo, min(hi, int(raw)))
    except (TypeError, ValueError):
        return default


async def _city_choice_info(p: Principal) -> tuple[bool, str | None, str | None]:
    """`(city_choice, bound_city_label, city_hint)`. `city_choice` — известен КОНКРЕТНЫЙ
    город менеджера (модуль городов включён И есть привязка `Principal.city`); иначе
    `city_hint` объясняет фронту, почему переключатель города не рисуется (вместо мёртвой
    кнопки)."""
    if await cities_module_on() and p.city:
        return True, await city_label(normalize_city(p.city)), None
    return False, None, CITY_HINT_TEXT


def _in_scope(city: str | None, scope) -> bool:
    """Пункт «все города» (`city is None`) виден из ЛЮБОГО scope — та же семантика, что
    `_city_clause(..., include_null=True)` в SQL и `_card_out_of_scope` в боте."""
    if scope is None or city is None:
        return True
    code, exclude = scope
    if exclude:
        return city not in exclude
    return city == code


async def _load_in_scope(item_id: int, scope) -> dict:
    """Форма `miniapp/routers/admin_tasks.py::_load_in_scope` — вызывается ПЕРЕД каждой
    мутацией (PATCH/move/DELETE), не только на чтении (T-nxp-03)."""
    row = await get_faq_item(item_id)
    if row is None:
        raise HTTPException(404, {"reason": "not_found", "text": TEXT_NOT_FOUND})
    if not _in_scope(row.get("city"), scope):
        raise HTTPException(403, {"reason": "out_of_scope", "text": TEXT_OUT_OF_SCOPE})
    return row


async def _admin_row(row: dict, number: int, *, bound_label: str | None, city_choice: bool,
                      can_move_up: bool, can_move_down: bool) -> dict:
    """Строка списка/карточки менеджера по контракту `<interfaces>` плана 260906-nxp — тот же
    словарь в ответе GET и в `item` ответа мутаций."""
    city = row.get("city")
    label = await city_label(city) if city else None
    enabled = bool(row.get("enabled"))
    question = str(row.get("question") or "")
    who = "у всех городов" if label is None else f"у делегатов города «{label}»"
    if city_choice:
        city_toggle_label = "Для всех городов" if city else f"Только {bound_label}"
    else:
        city_toggle_label = None
    return {
        "id": row["id"],
        "number": number,
        "question": question,
        "answer": str(row.get("answer") or ""),
        "city_badge": city_badge(label),
        "is_general": city is None,
        "enabled": enabled,
        "status_text": "показывается делегатам" if enabled else "скрыт от делегатов",
        "toggle_label": "Скрыть" if enabled else "Показать",
        "city_toggle_label": city_toggle_label,
        "delete_confirm_text": f"«{question}» — пропадёт {who}. Отменить нельзя.",
        "can_move_up": can_move_up,
        "can_move_down": can_move_down,
    }


async def _full_item(item_id: int, p: Principal) -> dict | None:
    """Пересобирает строку из СВЕЖЕГО списка (номер и стрелки — по актуальному scope-порядку),
    используется после каждой мутации. `None` — пункт выпал из scope между чтением и ответом
    (защитный случай: собственный `_load_in_scope` уже проверил доступ раньше в том же
    запросе)."""
    scope = await applications.manager_scope(p.city)
    items = await list_faq_items(city_scope=scope)
    idx = next((i for i, r in enumerate(items) if r["id"] == item_id), None)
    if idx is None:
        return None
    city_choice, bound_label, _hint = await _city_choice_info(p)
    return await _admin_row(
        items[idx], idx + 1, bound_label=bound_label, city_choice=city_choice,
        can_move_up=idx > 0, can_move_down=idx < len(items) - 1,
    )


@router.get("/app/api/admin/faq")
async def admin_faq_list(
    offset: str | None = None,
    limit: str | None = None,
    p: Principal = Depends(require_cap("moderate_reg")),
    _: Principal = Depends(require_section("faq")),
) -> dict:
    off = _parse_admin_int(offset, 0, 0, 10_000_000)
    lim = _parse_admin_int(limit, ADMIN_LIMIT_DEFAULT, 1, ADMIN_LIMIT_MAX)

    scope = await applications.manager_scope(p.city)
    items = await list_faq_items(city_scope=scope)
    total = len(items)
    city_choice, bound_label, city_hint = await _city_choice_info(p)

    page = items[off: off + lim]
    rows = [
        await _admin_row(
            row, off + idx + 1, bound_label=bound_label, city_choice=city_choice,
            can_move_up=(off + idx) > 0, can_move_down=(off + idx) < total - 1,
        )
        for idx, row in enumerate(page)
    ]
    return {
        "items": rows,
        "total": total,
        "offset": off,
        "limit": lim,
        "empty_text": ADMIN_EMPTY_TEXT,
        "city_choice": city_choice,
        "bound_city_label": bound_label,
        "city_hint": city_hint,
        "question_max": QUESTION_MAX,
        "answer_max": ANSWER_MAX,
    }


class AdminFaqCreate(BaseModel):
    question: str = ""
    answer: str = ""


@router.post("/app/api/admin/faq")
async def admin_faq_create(
    body: AdminFaqCreate,
    p: Principal = Depends(require_cap("moderate_reg")),
    _: Principal = Depends(require_section("faq")),
) -> dict:
    result = await _create_checked(
        question=body.question, answer=body.answer, city=p.city or None, created_by=p.telegram_id,
    )
    if result["ok"]:
        result["item"] = await _full_item(result["id"], p)
    return result


class AdminFaqPatch(BaseModel):
    question: str | None = None
    answer: str | None = None
    enabled: bool | None = None
    city: str | None = None


@router.patch("/app/api/admin/faq/{item_id}")
async def admin_faq_patch(
    item_id: int,
    body: AdminFaqPatch,
    p: Principal = Depends(require_cap("moderate_reg")),
    _: Principal = Depends(require_section("faq")),
) -> dict:
    fields = [name for name in ("question", "answer", "enabled", "city") if getattr(body, name) is not None]
    if len(fields) != 1:
        raise HTTPException(400, {"reason": "one_field", "text": TEXT_ONE_FIELD})
    field = fields[0]

    scope = await applications.manager_scope(p.city)
    await _load_in_scope(item_id, scope)

    if field == "question":
        question = body.question.strip()
        if not question:
            raise HTTPException(400, {"reason": "empty", "text": EMPTY_QUESTION_TEXT})
        if len(question) > QUESTION_MAX:
            raise HTTPException(400, {"reason": "too_long", "text": QUESTION_TOO_LONG_TEXT})
        await update_faq_item(item_id, question=question)
    elif field == "answer":
        answer = body.answer.strip()
        if not answer:
            raise HTTPException(400, {"reason": "empty", "text": EMPTY_ANSWER_TEXT})
        if len(answer) > ANSWER_MAX:
            raise HTTPException(400, {"reason": "too_long", "text": ANSWER_TOO_LONG_TEXT})
        await update_faq_item(item_id, answer=answer)
    elif field == "enabled":
        await update_faq_item(item_id, enabled=1 if body.enabled else 0)
    else:  # city — только "all"/"mine", код города фронт прислать не может (T-nxp-02)
        if body.city == "all":
            await update_faq_item(item_id, city=None)
        elif body.city == "mine":
            city_choice, _bound_label, _hint = await _city_choice_info(p)
            if not city_choice:
                raise HTTPException(400, {"reason": "no_city_binding", "text": TEXT_NO_CITY_BINDING})
            await update_faq_item(item_id, city=p.city)
        else:
            raise HTTPException(400, {"reason": "bad_city", "text": TEXT_BAD_CITY})

    return {"ok": True, "field": field, "item": await _full_item(item_id, p)}


class AdminFaqMove(BaseModel):
    direction: str = ""


@router.post("/app/api/admin/faq/{item_id}/move")
async def admin_faq_move(
    item_id: int,
    body: AdminFaqMove,
    p: Principal = Depends(require_cap("moderate_reg")),
    _: Principal = Depends(require_section("faq")),
) -> dict:
    """Дословно `handlers/admin_faq.py::_afaq_move`, но от scope менеджера. Пункт уже с краю
    списка -> `moved=false` — это не ошибка. Перестановка пишет позиции только видимому в
    scope подмножеству — порядок пунктов другого города доопределяется вторичным ключом `id`
    (документированное ограничение `reorder_faq_items`, database/db.py)."""
    if body.direction not in ("up", "down"):
        raise HTTPException(400, {"reason": "bad_direction", "text": TEXT_BAD_DIRECTION})

    scope = await applications.manager_scope(p.city)
    await _load_in_scope(item_id, scope)

    items = await list_faq_items(city_scope=scope)
    ids = [r["id"] for r in items]
    idx = ids.index(item_id)
    new_idx = idx + (-1 if body.direction == "up" else 1)
    moved = False
    if 0 <= new_idx < len(ids):
        ids[idx], ids[new_idx] = ids[new_idx], ids[idx]
        await reorder_faq_items(ids)
        moved = True
    return {"ok": True, "moved": moved}


@router.delete("/app/api/admin/faq/{item_id}")
async def admin_faq_delete(
    item_id: int,
    p: Principal = Depends(require_cap("moderate_reg")),
    _: Principal = Depends(require_section("faq")),
) -> dict:
    scope = await applications.manager_scope(p.city)
    await _load_in_scope(item_id, scope)
    await delete_faq_item(item_id)
    return {"ok": True, "deleted": True}


__all__ = ["router"]
