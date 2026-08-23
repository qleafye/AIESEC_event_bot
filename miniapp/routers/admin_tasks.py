"""Phase 19 (D-04 волна 2): задания менеджера из Mini App — список, карточка, точечные
правки, создание, архив/возврат/удаление. Зеркало экранов 6 и 7 скетча Phase 16 на ТЕХ ЖЕ
аксессорах, что бот (`handlers/admin_gamification.py`, `handlers/admin_game_tasks.py` —
импортировать нельзя, aiogram): `list_all_tasks`, `create_task`, `update_task_*`,
`archive_task`/`unarchive_task`, `delete_task` (у задания без сдач — гейт внутри SQL).

Правила — как у бота, без расхождений:
- точечная правка = РОВНО одно поле за PATCH (как `GameTaskEdit` правит одно поле за раз);
- название схлопывает пробелы/переносы и режется до 60 (`_normalize_task_title`);
- монеты — положительное целое (`_parse_positive_int`); дедлайн — ДД.ММ.ГГГГ ЧЧ:ММ либо
  пресет «сегодня 23:59 / +3 дня / +7 дней», в будущем по МОСКОВСКОМУ времени (TZFIX-260816);
- привязанный к городу менеджер создаёт задания только своему городу — поле города из тела
  игнорируется (`_bound_task_city`: суперадмин не ограничен никогда);
- обложка — `photo_file_id` только из `POST /app/api/uploads` с `part_token` того же
  менеджера (T-19-37), иначе обложкой можно было бы назначить чужой файл.

Городской скоуп (T-19-35) — на чтении и на КАЖДОЙ мутации: задание чужого города -> 403.
Задание «всем городам» (`event_city IS NULL`) в скоупе у любого менеджера — как в списке
бота (`list_all_tasks(include_null=True)`).

Любое изменение -> `enqueue("task_changed", {task_id})` — Sheets пересобирает бот (D-01).
Коды (категорий, типов подтверждения, городов) человеку не показываются: `GET …/options`
отдаёт пары `{code, label}`, фронт рисует подписи и возвращает код.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from cities import cities_module_on, city_codes, city_label, city_scope, enabled_cities, normalize_city
from database.db import (
    GAME_CATEGORIES,
    GAME_PROOF_TYPES,
    archive_task,
    count_task_submissions,
    count_task_submissions_by_status,
    create_task,
    delete_task,
    get_task,
    list_all_tasks,
    task_title,
    unarchive_task,
    update_task_coins,
    update_task_deadline,
    update_task_photo,
    update_task_text,
    update_task_title,
)
from game_labels import category_label, proof_types_label, render_task_card_text, task_deadline_short

from miniapp.deps import Principal, require_cap, require_section
from miniapp.outbox import enqueue
from miniapp.routers.submissions import check_part_token
from miniapp.routers.tasks import parse_page

router = APIRouter()

MOSCOW_TZ = ZoneInfo("Europe/Moscow")
TITLE_MAX = 60          # handlers/admin_gamification.py::_normalize_task_title
TEXT_MAX = 4000         # описание уходит в <blockquote> сообщения Telegram (4096)
COINS_MAX = 100_000
STORAGE_FMT = "%Y-%m-%d %H:%M:%S"
INPUT_FMT = "%d.%m.%Y %H:%M"

# Пресеты дедлайна — код -> подпись (handlers/game_task_wizard.py::_DEADLINE_PRESETS).
DEADLINE_PRESETS = (("today", "Сегодня 23:59"), ("plus3", "+3 дня"), ("plus7", "+7 дней"))
_PRESET_DAYS = {"today": 0, "plus3": 3, "plus7": 7}

# Тексты ошибок — человеку, с примером, что сделать (CLAUDE.md).
TEXT_ONE_FIELD = "За один раз меняется одно поле — название, описание, монеты, дедлайн или фото."
TEXT_TITLE_EMPTY = "Название не может быть пустым — например «Фото со стендом»."
TEXT_TEXT_EMPTY = "Описание не может быть пустым — напишите, что нужно сделать."
TEXT_BAD_COINS = "Монеты — целое число больше нуля, например 10."
TEXT_BAD_DEADLINE = "Не понял дату. Формат: ДД.ММ.ГГГГ ЧЧ:ММ, например 25.08.2026 23:59."
TEXT_DEADLINE_PAST = "Это время уже прошло — выберите будущую дату."
TEXT_BAD_CATEGORY = "Выберите категорию из списка."
TEXT_BAD_PROOF = "Типы подтверждения — только из списка."
TEXT_BAD_CITY = "Выберите город из списка или «Все города»."
TEXT_BAD_PHOTO = "Фото не принято — загрузите его заново из приложения."
TEXT_NOT_A_PHOTO = "Обложка должна быть картинкой — файл другого типа не подойдёт."
TEXT_HAS_SUBMISSIONS = "У задания есть сдачи — по нему уже есть история. Его можно только убрать в архив."
TEXT_OUT_OF_SCOPE = "Это задание другого города — переключите город."
TEXT_NOT_FOUND = "Задание не найдено — возможно, его уже удалили."


# ── время и разбор полей (копии чистых хелперов бота — импорт тянет aiogram) ───────────

def now_moscow_naive() -> datetime:
    """services/scheduler.py::_now_moscow_naive — ввод менеджера сравнивается с МОСКОВСКИМ
    временем, а не с часами контейнера (UTC)."""
    return datetime.now(MOSCOW_TZ).replace(tzinfo=None)


def resolve_deadline_preset(code: str) -> datetime | None:
    """handlers/game_task_wizard.py::_resolve_deadline_preset: неизвестный код -> None."""
    days = _PRESET_DAYS.get(code)
    if days is None:
        return None
    base = now_moscow_naive().replace(hour=23, minute=59, second=0, microsecond=0)
    return base + timedelta(days=days)


def parse_deadline(raw) -> tuple[str | None, str | None]:
    """`(deadline_at в формате хранения, reason)`: пресет, ДД.ММ.ГГГГ ЧЧ:ММ или уже формат
    хранения; прошлое -> `deadline_past`, мусор -> `bad_deadline`."""
    if not isinstance(raw, str) or not raw.strip():
        return None, "bad_deadline"
    raw = raw.strip()
    when = resolve_deadline_preset(raw)
    if when is None:
        for fmt in (INPUT_FMT, STORAGE_FMT):
            try:
                when = datetime.strptime(raw, fmt)
                break
            except ValueError:
                continue
    if when is None:
        return None, "bad_deadline"
    if when <= now_moscow_naive():
        return None, "deadline_past"
    return when.strftime(STORAGE_FMT), None


def normalize_task_title(raw) -> str:
    """handlers/admin_gamification.py::_normalize_task_title — перенос -> пробел, 60 символов."""
    return " ".join(str(raw or "").split())[:TITLE_MAX]


def parse_positive_int(raw) -> int | None:
    if isinstance(raw, bool):
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if 1 <= value <= COINS_MAX else None


# ── скоуп ────────────────────────────────────────────────────────────────────────────────

async def bound_city(request: Request, p: Principal) -> str | None:
    """handlers/admin_gamification.py::_bound_task_city: суперадмин из ADMIN_IDS не
    ограничен никогда; модуль городов выключен -> ограничений нет."""
    if p.telegram_id in (request.app.state.cfg.admin_ids or ()):
        return None
    if not await cities_module_on() or not p.city:
        return None
    return normalize_city(p.city)


def task_in_scope(task: dict, bound: str | None) -> bool:
    if bound is None:
        return True
    if not task.get("event_city"):
        return True  # «всем городам» видно из любого города — как list_all_tasks(include_null=True)
    return normalize_city(task["event_city"]) == bound


async def _load_in_scope(request: Request, p: Principal, task_id: int) -> tuple[dict, str | None]:
    task = await get_task(task_id)
    if task is None:
        raise HTTPException(404, {"reason": "not_found", "text": TEXT_NOT_FOUND})
    bound = await bound_city(request, p)
    if not task_in_scope(task, bound):
        raise HTTPException(403, {"reason": "out_of_scope", "text": TEXT_OUT_OF_SCOPE})
    return task, bound


async def _changed(task_id: int) -> None:
    await enqueue("task_changed", {"task_id": task_id})


def _require_photo_token(request: Request, p: Principal, file_id: str, token) -> None:
    """Обложка только из POST /uploads того же менеджера (T-19-37): подпись ставится по
    `kind` файла — документ подписан как `document` и обложкой стать не может."""
    secret = request.app.state.cfg.session_secret
    if check_part_token(secret, p.telegram_id, "photo", file_id, token):
        return
    if check_part_token(secret, p.telegram_id, "document", file_id, token):
        raise HTTPException(400, {"reason": "not_a_photo", "text": TEXT_NOT_A_PHOTO})
    raise HTTPException(403, {"reason": "bad_part_token", "text": TEXT_BAD_PHOTO})


# ── вывод ────────────────────────────────────────────────────────────────────────────────

async def _row(task: dict, number: int, counts: dict) -> dict:
    deadline_short, overdue = task_deadline_short(task)
    c = counts.get(task["id"], {})
    return {
        "id": task["id"],
        "number": number,
        "title": task_title(task),
        "category": task["category"],
        "category_label": await category_label(str(task["category"])),
        "coins": task["coins"],
        "deadline_at": task["deadline_at"],
        "deadline_short": deadline_short,
        "overdue": overdue,
        "archived": bool(task.get("archived_at")),
        "pending": c.get("pending", 0),
        "approved": c.get("approved", 0),
        "has_photo": bool(task.get("photo_file_id")),
    }


def _deadline_display(task: dict) -> str:
    try:
        return datetime.strptime(task["deadline_at"], STORAGE_FMT).strftime("%d.%m.%Y %H:%M")
    except (TypeError, ValueError):
        return str(task.get("deadline_at") or "—")


async def _card(task: dict) -> dict:
    submissions = await count_task_submissions(task["id"])
    city = task.get("event_city")
    return {
        "id": task["id"],
        "title": task_title(task),
        "text": task["text"],
        "category": task["category"],
        "category_label": await category_label(str(task["category"])),
        "coins": task["coins"],
        "deadline_at": task["deadline_at"],
        "deadline_display": _deadline_display(task),
        "proof_type": task.get("proof_type"),
        "proof_label": await proof_types_label(task.get("proof_type")),
        "event_city": city,
        "city_label": (await city_label(normalize_city(city))) if city and await cities_module_on() else None,
        "photo_file_id": task.get("photo_file_id"),
        "archived": bool(task.get("archived_at")),
        "archived_at": task.get("archived_at"),
        "submissions_count": submissions,
        "can_delete": submissions == 0,
        "cannot_delete_text": None if submissions == 0 else TEXT_HAS_SUBMISSIONS,
        # Тот же текст, что видит делегат («👁 Как видит делегат» бесплатно).
        "card_text": await render_task_card_text(task, "новое", None),
    }


# ── GET /app/api/admin/tasks/options ─────────────────────────────────────────────────────

@router.get("/app/api/admin/tasks/options")
async def task_options(
    request: Request,
    p: Principal = Depends(require_cap("moderate_game")),
    _: Principal = Depends(require_section("admin_tasks")),
) -> dict:
    """Справочники для экрана создания: подписи приходят с сервера, фронт кодов не знает."""
    bound = await bound_city(request, p)
    cities = []
    if await cities_module_on() and bound is None:
        cities = [{"code": c["code"], "label": await city_label(c["code"])} for c in await enabled_cities()]
    return {
        "categories": [{"code": c, "label": await category_label(c)} for c in GAME_CATEGORIES],
        "proof_types": [{"code": c, "label": await proof_types_label(c)} for c in GAME_PROOF_TYPES],
        "deadline_presets": [{"code": c, "label": label} for c, label in DEADLINE_PRESETS],
        "deadline_example": (now_moscow_naive() + timedelta(days=3)).replace(hour=23, minute=59).strftime(INPUT_FMT),
        "cities": cities,
        "city_choice": bool(cities),
        "bound_city_label": (await city_label(bound)) if bound else None,
        "title_max": TITLE_MAX,
        "text_max": TEXT_MAX,
    }


# ── GET /app/api/admin/tasks ─────────────────────────────────────────────────────────────

@router.get("/app/api/admin/tasks")
async def admin_tasks_list(
    request: Request,
    archived: str | None = None, offset: str | None = None, limit: str | None = None,
    p: Principal = Depends(require_cap("moderate_game")),
    _: Principal = Depends(require_section("admin_tasks")),
) -> dict:
    bound = await bound_city(request, p)
    want_archived = str(archived or "0").strip().lower() in ("1", "true", "yes")
    all_tasks = await list_all_tasks(city_scope=city_scope(bound))
    wanted = [t for t in all_tasks if bool(t.get("archived_at")) == want_archived]
    off, lim = parse_page(offset, limit)
    page = wanted[off:off + lim]
    counts = await count_task_submissions_by_status([t["id"] for t in page])
    items = [await _row(t, off + i + 1, counts) for i, t in enumerate(page)]
    return {
        "items": items,
        "total": len(wanted),
        "active_count": len(all_tasks) - sum(1 for t in all_tasks if t.get("archived_at")),
        "archived_count": sum(1 for t in all_tasks if t.get("archived_at")),
        "archived": want_archived,
        "offset": off,
        "limit": lim,
        "empty_text": "Архив пуст." if want_archived else "Заданий пока нет.",
    }


# ── GET /app/api/admin/tasks/{task_id} ───────────────────────────────────────────────────

@router.get("/app/api/admin/tasks/{task_id}")
async def admin_task_card(
    task_id: int, request: Request,
    p: Principal = Depends(require_cap("moderate_game")),
    _: Principal = Depends(require_section("admin_tasks")),
) -> dict:
    task, _bound = await _load_in_scope(request, p, task_id)
    return await _card(task)


# ── PATCH /app/api/admin/tasks/{task_id} ─────────────────────────────────────────────────

class TaskPatch(BaseModel):
    title: str | None = None
    text: str | None = None
    coins: int | None = None
    deadline_at: str | None = None
    photo_file_id: str | None = None
    remove_photo: bool = False
    part_token: str | None = None


@router.patch("/app/api/admin/tasks/{task_id}")
async def admin_task_patch(
    task_id: int, body: TaskPatch, request: Request,
    p: Principal = Depends(require_cap("moderate_game")),
    _: Principal = Depends(require_section("admin_tasks")),
) -> dict:
    fields = [name for name in ("title", "text", "coins", "deadline_at", "photo_file_id")
              if getattr(body, name) is not None]
    if body.remove_photo:
        fields.append("remove_photo")
    if len(fields) != 1:
        raise HTTPException(400, {"reason": "one_field", "text": TEXT_ONE_FIELD})
    field = fields[0]
    task, _bound = await _load_in_scope(request, p, task_id)

    if field == "title":
        title = normalize_task_title(body.title)
        if not title:
            raise HTTPException(400, {"reason": "title_empty", "text": TEXT_TITLE_EMPTY})
        ok = await update_task_title(task_id, title)
    elif field == "text":
        text = str(body.text).strip()[:TEXT_MAX]
        if not text:
            raise HTTPException(400, {"reason": "text_empty", "text": TEXT_TEXT_EMPTY})
        ok = await update_task_text(task_id, text)
    elif field == "coins":
        coins = parse_positive_int(body.coins)
        if coins is None:
            raise HTTPException(400, {"reason": "bad_coins", "text": TEXT_BAD_COINS})
        ok = await update_task_coins(task_id, coins)
    elif field == "deadline_at":
        deadline, reason = parse_deadline(body.deadline_at)
        if reason:
            raise HTTPException(400, {"reason": reason, "text": TEXT_DEADLINE_PAST if reason == "deadline_past" else TEXT_BAD_DEADLINE})
        ok = await update_task_deadline(task_id, deadline)
    elif field == "photo_file_id":
        file_id = str(body.photo_file_id).strip()
        if not file_id:
            raise HTTPException(403, {"reason": "bad_part_token", "text": TEXT_BAD_PHOTO})
        _require_photo_token(request, p, file_id, body.part_token)
        ok = await update_task_photo(task_id, file_id)
    else:  # remove_photo — неразрушительно, как «🗑 Убрать фото» в боте (без подтверждения)
        ok = await update_task_photo(task_id, None)

    if not ok:
        raise HTTPException(404, {"reason": "not_found", "text": TEXT_NOT_FOUND})
    await _changed(task_id)
    return {"ok": True, "field": field, "task": await _card(await get_task(task_id))}


# ── POST /app/api/admin/tasks ────────────────────────────────────────────────────────────

class TaskCreate(BaseModel):
    title: str = ""
    text: str = ""
    category: str = ""
    coins: int | None = None
    proof_types: list[str] = Field(default_factory=list)
    deadline_at: str = ""
    event_city: str | None = None
    photo_file_id: str | None = None
    part_token: str | None = None


@router.post("/app/api/admin/tasks", status_code=201)
async def admin_task_create(
    body: TaskCreate, request: Request,
    p: Principal = Depends(require_cap("moderate_game")),
    _: Principal = Depends(require_section("admin_tasks")),
) -> dict:
    title = normalize_task_title(body.title)
    if not title:
        raise HTTPException(400, {"reason": "title_empty", "text": TEXT_TITLE_EMPTY})
    text = body.text.strip()[:TEXT_MAX]
    if not text:
        raise HTTPException(400, {"reason": "text_empty", "text": TEXT_TEXT_EMPTY})
    if body.category not in GAME_CATEGORIES:
        raise HTTPException(400, {"reason": "bad_category", "text": TEXT_BAD_CATEGORY})
    coins = parse_positive_int(body.coins)
    if coins is None:
        raise HTTPException(400, {"reason": "bad_coins", "text": TEXT_BAD_COINS})
    if any(code not in GAME_PROOF_TYPES for code in body.proof_types):
        raise HTTPException(400, {"reason": "bad_proof_type", "text": TEXT_BAD_PROOF})
    # Порядок хранения — как у бота (gtproof_done): по GAME_PROOF_TYPES, пусто = «не важно».
    proof_type = ",".join(c for c in GAME_PROOF_TYPES if c in set(body.proof_types))
    deadline, reason = parse_deadline(body.deadline_at)
    if reason:
        raise HTTPException(400, {"reason": reason, "text": TEXT_DEADLINE_PAST if reason == "deadline_past" else TEXT_BAD_DEADLINE})

    # Город: привязанному менеджеру — только свой (поле из тела игнорируется, как в боте);
    # модуль выключен — «всем»; иначе None/«all» = всем либо код из включённых городов.
    bound = await bound_city(request, p)
    if bound is not None:
        event_city: str | None = bound
    elif not await cities_module_on():
        event_city = None
    else:
        raw_city = (body.event_city or "").strip()
        if raw_city in ("", "all"):
            event_city = None
        else:
            enabled = {c["code"] for c in await enabled_cities()}
            if raw_city not in city_codes() or raw_city not in enabled:
                raise HTTPException(400, {"reason": "bad_city", "text": TEXT_BAD_CITY})
            event_city = raw_city

    photo_file_id = (body.photo_file_id or "").strip() or None
    if photo_file_id:
        _require_photo_token(request, p, photo_file_id, body.part_token)

    task_id = await create_task(
        text, body.category, coins, proof_type, deadline, p.telegram_id,
        event_city=event_city, title=title, photo_file_id=photo_file_id,
    )
    await _changed(task_id)
    return {"ok": True, "id": task_id, "task": await _card(await get_task(task_id))}


# ── архив / возврат / удаление ───────────────────────────────────────────────────────────

@router.post("/app/api/admin/tasks/{task_id}/archive")
async def admin_task_archive(
    task_id: int, request: Request,
    p: Principal = Depends(require_cap("moderate_game")),
    _: Principal = Depends(require_section("admin_tasks")),
) -> dict:
    await _load_in_scope(request, p, task_id)
    changed = await archive_task(task_id)
    if changed:
        await _changed(task_id)
    return {"ok": True, "changed": changed, "archived": True}


@router.post("/app/api/admin/tasks/{task_id}/unarchive")
async def admin_task_unarchive(
    task_id: int, request: Request,
    p: Principal = Depends(require_cap("moderate_game")),
    _: Principal = Depends(require_section("admin_tasks")),
) -> dict:
    await _load_in_scope(request, p, task_id)
    changed = await unarchive_task(task_id)
    if changed:
        await _changed(task_id)
    return {"ok": True, "changed": changed, "archived": False}


@router.delete("/app/api/admin/tasks/{task_id}")
async def admin_task_delete(
    task_id: int, request: Request,
    p: Principal = Depends(require_cap("moderate_game")),
    _: Principal = Depends(require_section("admin_tasks")),
) -> dict:
    await _load_in_scope(request, p, task_id)
    if await count_task_submissions(task_id) > 0:
        raise HTTPException(409, {"reason": "has_submissions", "text": TEXT_HAS_SUBMISSIONS})
    # T-14-02: сам гейт «без сдач» — внутри DELETE; гонка со сдачей -> False при живой строке.
    if not await delete_task(task_id):
        if await get_task(task_id) is not None:
            raise HTTPException(409, {"reason": "has_submissions", "text": TEXT_HAS_SUBMISSIONS})
        raise HTTPException(404, {"reason": "not_found", "text": TEXT_NOT_FOUND})
    await _changed(task_id)
    return {"ok": True, "deleted": True}


__all__ = ["router", "parse_deadline", "normalize_task_title", "resolve_deadline_preset"]
